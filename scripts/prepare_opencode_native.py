import json
from pathlib import Path

import click
from native_agent import (
    harness_agent,
    provider_options,
    runtime_and_evidence,
    save_case,
    selected_provider,
)

from overnight.adapters.provider import provider_record
from overnight.domain.contracts import JsonObject

CONTAINER_RUNTIME = "/opt/opencode"
REQUIRED: JsonObject = {
    "simulated_clock": False,
    "chat_isolation": True,
    "cancel": True,
    "reset_scope": "chat",
}


@click.command()
@click.option("--runtime", type=click.Path(exists=True, path_type=Path))
@click.option("--container", help="Run the agent in this image, such as overnight-opencode.")
@click.option(
    "--fixture",
    type=click.Choice(("native-capabilities", "native-delegation", "native-cancellation")),
    default="native-capabilities",
    show_default=True,
)
@provider_options
@click.option("--model", default="overnight/openai/gpt-6-luna", show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
def main(
    runtime: Path | None,
    container: str | None,
    fixture: str,
    base_url: str | None,
    api_key_env: str | None,
    reasoning_effort: str | None,
    model: str,
    out: Path,
) -> None:
    out = out.resolve()
    provider = selected_provider(base_url, api_key_env, reasoning_effort)
    runtime_path, evidence = runtime_and_evidence(runtime, container, CONTAINER_RUNTIME, out)
    config: JsonObject = {
        "factory": "overnight_opencode:adapter",
        "evidence": evidence,
        "options": {
            "runtime": runtime_path,
            "provider": provider_record(provider),
            "model": model,
            "input_usd_per_million": 0.2,
            "output_usd_per_million": 0.75,
        },
        "timeout_seconds": 180,
    }
    source = Path(__file__).resolve().parents[1] / "examples" / "fixtures" / f"{fixture}.json"
    scenario = json.loads(source.read_text())
    scenario["agent"] = harness_agent(
        config, out, container, provider, required=REQUIRED, timeout_seconds=180
    )
    click.echo(str(save_case(scenario, config, out, container)))


if __name__ == "__main__":
    main()
