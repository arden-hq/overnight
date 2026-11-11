import json
import os
import shlex
import shutil
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, model_validator

from overnight.adapters.config import Factory, Launch, SessionContext
from overnight.adapters.evidence import source_identity
from overnight.adapters.files import write_private_json
from overnight.adapters.http import serve_http
from overnight.adapters.provider import Provider, api_key, endpoint, provider_record
from overnight.adapters.provider_relay import ProviderRelay, RequestBudget, run_capture
from overnight.domain.contracts import JsonObject
from overnight.execution.harness import Capabilities
from overnight.execution.provenance import ModelConfiguration
from overnight_pi.profile import NativeProfile


class PackageMetadata(BaseModel):
    version: str


RELAY_KEY_ENV = "OVERNIGHT_PI_PROXY_KEY"


class PiOptions(RequestBudget):
    runtime: Path = Field(strict=False)
    provider: Provider = "openrouter"
    native: NativeProfile = Field(default_factory=NativeProfile)
    model: str = "openai/gpt-6-luna"
    reasoning: bool = False

    @model_validator(mode="after")
    def require_reasoning_model(self):
        if self.native.thinking != "off" and not self.reasoning:
            raise ValueError("thinking requires a reasoning-capable model definition")
        return self


def _reasoning(options: PiOptions) -> JsonObject:
    off = endpoint(options.provider).reasoning_effort
    if off is None:
        return {"reasoning": options.reasoning}
    return {
        "reasoning": True,
        "thinkingLevelMap": {"off": off},
        "compat": {"supportsReasoningEffort": True},
    }


async def prepare(context: SessionContext) -> Launch:
    options = PiOptions.model_validate(context.options)
    secret = api_key(endpoint(options.provider))
    runtime = options.runtime.resolve()
    node = shutil.which("node")
    if node is None:
        raise ValueError("Pi requires Node.js")
    modules = runtime / "node_modules"
    pi = modules / "@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    acp = modules / "pi-acp/dist/index.js"
    if not pi.is_file() or not acp.is_file():
        raise ValueError("install the pinned Pi runtime with npm ci first")
    root = context.directory
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    agent = root / "pi"
    agent.mkdir(mode=0o700)
    bindings = root / "bindings"
    bindings.mkdir(mode=0o700)
    binding = root / "tools.mjs"
    source = Path(__file__).with_name("tools.mjs").read_text()
    source = source.replace(
        "@modelcontextprotocol/sdk/",
        (modules / "@modelcontextprotocol/sdk/dist/esm").as_uri() + "/",
    )
    descriptor = os.open(binding, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(source)
    write_private_json(
        root / "tools.json",
        {
            "binding_receipts": str(bindings),
            "chat_id": context.chat_id,
            "endpoint": context.endpoint,
            "token": context.token,
            "operations": {
                service: [operation.model_dump(mode="json") for operation in operations]
                for service, operations in context.operations.items()
            },
        },
    )
    write_private_json(
        agent / "settings.json",
        {
            "quietStartup": True,
            "compaction": {"enabled": options.native.compaction},
            "retry": {"enabled": False},
            "defaultTools": [str(tool) for tool in options.native.builtin_tools],
        },
    )

    command = [
        node,
        str(pi),
        "--provider",
        "overnight",
        "--model",
        options.model,
        *options.native.arguments(),
        "--extension",
        str(binding),
    ]
    launcher = root / "pi-launch"
    descriptor = os.open(launcher, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w") as output:
        output.write(f'#!/bin/sh\nexec {shlex.join(command)} "$@"\n')
    runtime_code = source_identity(
        modules,
        ";".join(
            f"{name}={PackageMetadata.model_validate_json((modules / name / 'package.json').read_bytes()).version}"
            for name in (
                "@earendil-works/pi-coding-agent",
                "pi-acp",
                "@modelcontextprotocol/sdk",
            )
        ),
    )

    def record(value):
        with (root / "provider.jsonl").open("a") as output:
            output.write(json.dumps(value) + "\n")

    os.close(os.open(root / "provider.jsonl", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    stack = AsyncExitStack()
    try:
        client = await stack.enter_async_context(httpx.AsyncClient(timeout=90))
        relay = ProviderRelay(
            options,
            options.provider,
            options.model,
            secret,
            client,
            record,
            capture=run_capture(),
        )
        relay_url = await stack.enter_async_context(serve_http(relay.app))
        write_private_json(
            agent / "models.json",
            {
                "providers": {
                    "overnight": {
                        "baseUrl": relay_url + "/v1",
                        "api": "openai-completions",
                        "apiKey": f"${RELAY_KEY_ENV}",
                        "authHeader": True,
                        "models": [
                            {
                                "id": options.model,
                                "name": options.model,
                                **_reasoning(options),
                                "input": ["text"],
                                "contextWindow": 262144,
                                "maxTokens": options.max_tokens,
                            }
                        ],
                    }
                }
            },
        )
    except BaseException:
        await stack.aclose()
        raise

    async def close():
        record({"kind": "provider_closed", "requests": relay.requests})
        await stack.aclose()

    return Launch(
        adapter_code=source_identity(Path(__file__).parent, "0.1.0"),
        runtime_code=runtime_code,
        model=ModelConfiguration(
            name=options.model,
            uses_runtime_defaults=True,
            settings={
                "provider": provider_record(options.provider),
                "max_tokens": options.max_tokens,
                "max_requests": options.max_requests,
                "max_request_bytes": options.max_request_bytes,
                "max_cost_usd": options.max_cost_usd,
                "input_usd_per_million": options.input_usd_per_million,
                "output_usd_per_million": options.output_usd_per_million,
                "native": options.native.model_dump(mode="json"),
            },
        ),
        command=[node, str(acp)],
        cwd=workspace,
        env={
            "HOME": str(root),
            "PI_CODING_AGENT_DIR": str(agent),
            "PI_ACP_PI_COMMAND": str(launcher),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "OVERNIGHT_PI_TOOLS": str(root / "tools.json"),
            RELAY_KEY_ENV: relay.token,
        },
        close=close,
    )


adapter = Factory(
    prepare=prepare,
    capabilities=Capabilities(chat_isolation=True, cancel=True, reset_scope="chat"),
)
