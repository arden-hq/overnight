import asyncio
import hashlib
import os
from functools import partial
from pathlib import Path

import click
import feather_harness
from openai import AsyncOpenAI

from overnight.adapters.evidence import source_identity
from overnight.adapters.host import serve
from overnight.adapters.protocol import Lifecycle
from overnight.adapters.provider import (
    CompatibleProvider,
    Provider,
    api_key,
    endpoint,
    provider_record,
)
from overnight.execution.harness import LifecycleReply
from overnight.execution.provenance import (
    CodeIdentity,
    ConnectorSidecar,
    ModelConfiguration,
)
from overnight_feather.options import NativeOptions
from overnight_feather.provider import Pricing
from overnight_feather.session import CAPABILITIES, Connector


async def main(
    model: str,
    provider: Provider,
    evidence: Path,
    max_tokens: int,
    max_cost: float,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
    native_config: Path | None = None,
) -> None:
    pricing = None
    if input_usd_per_million is not None or output_usd_per_million is not None:
        pricing = Pricing.model_validate(
            {
                "input_usd_per_million": input_usd_per_million,
                "output_usd_per_million": output_usd_per_million,
            }
        )
    native = (
        NativeOptions.model_validate_json(native_config.read_bytes())
        if native_config
        else NativeOptions()
    )
    route = endpoint(provider)
    key = api_key(route)
    # The client holds the key; nothing Feather starts, such as its shell worker, needs it.
    os.environ.pop(route.api_key_env, None)
    write_provenance(model, provider, max_tokens, pricing, native)
    namespace = os.environ.get("OVERNIGHT_CHAT_NAMESPACE")
    if namespace is not None:
        evidence = evidence / "chats" / namespace
    async with AsyncOpenAI(
        api_key=key, base_url=route.base_url, timeout=120, max_retries=0
    ) as client:
        connector = Connector(client, model, evidence, max_tokens, max_cost, pricing, native)

        await serve(
            CAPABILITIES, connector.observe, partial(apply_lifecycle, connector), connector.close
        )


async def apply_lifecycle(connector: Connector, message: Lifecycle) -> LifecycleReply:
    if message.kind == "reset" and message.scope != "chat":
        raise ValueError("Feather reset supports chat scope only")
    chat = connector.chats.get(message.chat_id)
    if message.kind == "close":
        await connector.close()
    elif chat is not None:
        if message.kind == "reset":
            await connector.remove(chat, reason="reset")
        else:
            await connector.cancel(chat)
    return LifecycleReply(kind=message.kind, scope=message.scope)


def write_provenance(
    model: str, provider: Provider, max_tokens: int, pricing: Pricing | None, native: NativeOptions
) -> None:
    target = os.environ.get("OVERNIGHT_PROVENANCE_PATH")
    if target is None:
        return
    runtime_root = Path(feather_harness.__file__).parent
    digest = hashlib.sha256()
    for source in sorted(runtime_root.rglob("*.py")):
        name = source.relative_to(runtime_root).as_posix().encode()
        contents = source.read_bytes()
        digest.update(f"{len(name)}:".encode() + name)
        digest.update(f"{len(contents)}:".encode() + contents)
    sidecar = ConnectorSidecar(
        implementation="overnight-native-feather",
        code=source_identity(Path(__file__).parent, "0.1.0"),
        runtime=CodeIdentity(
            git_commit="b7463092af862ebbdf4c5920c4bc9ab38ab17ad9",
            sha256=digest.hexdigest(),
            version="0.1.0",
        ),
        model=ModelConfiguration(
            name=model,
            settings={
                "provider": provider_record(provider),
                "native": native.model_dump(mode="json"),
                "effective_compaction": native.effective_compaction.model_dump(mode="json"),
                "pricing": (pricing or Pricing()).model_dump(mode="json"),
                "max_tokens": max_tokens,
                "reasoning_effort": "low",
                "parallel_tool_calls": False,
            },
            uses_runtime_defaults=False,
        ),
    )
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(sidecar.model_dump_json())


@click.command()
@click.option("--model", required=True)
@click.option(
    "--base-url",
    help="An OpenAI-compatible endpoint. Without it Feather uses OpenRouter and OPENROUTER_API_KEY.",
)
@click.option("--api-key-env", help="The variable holding the --base-url endpoint's key.")
@click.option("--evidence", type=click.Path(path_type=Path), required=True)
@click.option("--max-tokens", type=click.IntRange(1, 4096), default=4096)
@click.option("--max-cost", type=click.FloatRange(min=0, max=5, min_open=True), default=5)
@click.option("--input-usd-per-million", type=click.FloatRange(min=0, min_open=True))
@click.option("--output-usd-per-million", type=click.FloatRange(min=0, min_open=True))
@click.option("--native-config", type=click.Path(exists=True, path_type=Path))
def run(
    model: str,
    base_url: str | None,
    api_key_env: str | None,
    evidence: Path,
    max_tokens: int,
    max_cost: float,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
    native_config: Path | None = None,
) -> None:
    asyncio.run(
        main(
            model,
            _provider(base_url, api_key_env),
            evidence,
            max_tokens,
            max_cost,
            input_usd_per_million,
            output_usd_per_million,
            native_config,
        )
    )


def _provider(base_url: str | None, api_key_env: str | None) -> Provider:
    """The shared provider contract: the key always comes from the variable named for its host."""
    if base_url is None:
        if api_key_env is not None:
            raise click.UsageError("--api-key-env needs --base-url")
        return "openrouter"
    if api_key_env is None:
        raise click.UsageError("--base-url needs --api-key-env")
    try:
        return CompatibleProvider(base_url=base_url, api_key_env=api_key_env)
    except ValueError as error:
        raise click.UsageError(str(error)) from None
