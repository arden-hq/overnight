import json
import sys
from collections.abc import Callable
from pathlib import Path

import click
from pydantic import ValidationError

from overnight.adapters.files import write_private_json
from overnight.adapters.provider import CompatibleProvider, Provider, endpoint
from overnight.domain.contracts import JsonObject
from overnight.execution.scenario import resolve_scenario

CONTAINER_EVIDENCE = "/overnight/agent/native"


def provider_options[F: Callable](command: F) -> F:
    """The model provider: OpenRouter, or an OpenAI-compatible endpoint given at run time."""
    for option in reversed(
        [
            click.option(
                "--base-url",
                help="An OpenAI-compatible endpoint, such as https://api.example.test/v1. "
                "Without it the agent uses OpenRouter and OPENROUTER_API_KEY.",
            ),
            click.option("--api-key-env", help="The variable holding the endpoint's key."),
            click.option(
                "--reasoning-effort",
                help="The reasoning effort every request states, for an endpoint that needs one.",
            ),
        ]
    ):
        command = option(command)
    return command


def selected_provider(
    base_url: str | None, api_key_env: str | None, reasoning_effort: str | None
) -> Provider:
    if base_url is None:
        if api_key_env is not None or reasoning_effort is not None:
            raise click.UsageError("--api-key-env and --reasoning-effort need --base-url")
        return "openrouter"
    if api_key_env is None:
        raise click.UsageError("--base-url needs --api-key-env")
    try:
        return CompatibleProvider(
            base_url=base_url, api_key_env=api_key_env, reasoning_effort=reasoning_effort
        )
    except ValidationError as error:
        raise click.UsageError(str(error)) from None


def runtime_and_evidence(
    runtime: Path | None, container: str | None, image_runtime: str, out: Path
) -> tuple[str, str]:
    if (runtime is None) == (container is None):
        raise click.UsageError("pass --runtime for a host run or --container for a contained one")
    if runtime is None:
        return image_runtime, CONTAINER_EVIDENCE
    return str(runtime.resolve()), str((out / "native").resolve())


def harness_agent(
    config: JsonObject,
    out: Path,
    container: str | None,
    provider: Provider,
    *,
    required: JsonObject,
    timeout_seconds: float,
) -> JsonObject:
    agent: JsonObject = {
        "kind": "harness",
        "required": required,
        "timeout_seconds": timeout_seconds,
    }
    module = ["-m", "overnight.adapters.acp"]
    if container is None:
        return {
            **agent,
            "run_on_host": True,
            "command": [sys.executable, *module, "--config", str(out / "adapter.json")],
        }
    route = endpoint(provider)
    return {
        **agent,
        "command": ["python3", *module, "--config-json", json.dumps(config)],
        "container": {"image": container, "env": [route.api_key_env], "allow_hosts": [route.host]},
    }


def save_case(scenario: JsonObject, config: JsonObject, out: Path, container: str | None) -> Path:
    resolve_scenario(scenario)
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    if container is None:
        write_private_json(out / "adapter.json", config)
    write_private_json(out / "scenario.json", scenario)
    return out / "scenario.json"
