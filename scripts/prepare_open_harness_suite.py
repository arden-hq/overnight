import json
import os
from pathlib import Path

import click
from native_agent import harness_agent, provider_options, selected_provider

from overnight.adapters.provider import provider_record
from overnight.domain.contracts import JsonObject

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EVIDENCE = "/overnight/agent/native"
RATES: JsonObject = {"input_usd_per_million": 0.2, "output_usd_per_million": 0.75}
WITHOUT_CLOCK: JsonObject = {
    "simulated_clock": False,
    "chat_isolation": True,
    "cancel": True,
    "reset_scope": "chat",
}
ARMS: dict[str, tuple[str, JsonObject, JsonObject]] = {
    "pi": (
        "overnight-pi",
        {
            "factory": "overnight_pi:adapter",
            "options": {"runtime": "/opt/pi", "max_requests": 40, "max_cost_usd": 3},
        },
        WITHOUT_CLOCK,
    ),
    "opencode": (
        "overnight-opencode",
        {
            "factory": "overnight_opencode:adapter",
            "options": {"runtime": "/opt/opencode"},
            "timeout_seconds": 180,
        },
        WITHOUT_CLOCK,
    ),
    "prime": (
        "overnight-prime",
        {
            "factory": "overnight_prime.launch:adapter",
            "options": {"runtime": "/opt/prime", "max_requests": 40, "max_cost_usd": 3},
        },
        WITHOUT_CLOCK | {"simulated_clock": True},
    ),
}
MODELS = {"opencode": "overnight/openai/gpt-6-luna"}
CASES = [
    ("capabilities", "fixtures/native-capabilities.json", None, None),
    ("delegation", "fixtures/native-delegation.json", None, None),
    ("cancellation", "fixtures/native-cancellation.json", None, None),
    ("catering", "catering-harness.json", None, None),
    ("catering-overrides", "catering-harness.json", "catering-overrides.json", None),
    (
        "catering-adversarial",
        "catering-harness.json",
        None,
        "catering-adversarial-variation.json",
    ),
    ("shared-world", "shared-world-chaos.json", None, None),
    ("shared-world-noise", "shared-world-chaos.json", None, "shared-world-chaos-variation.json"),
]


@click.command()
@provider_options
@click.option("--attempts", type=click.IntRange(1, 100), default=1, show_default=True)
@click.option("--arm", "arms", multiple=True, type=click.Choice(sorted(ARMS)))
@click.option("--case", "cases", multiple=True, type=click.Choice([case[0] for case in CASES]))
@click.option("--out", required=True, type=click.Path(path_type=Path), help="The suite file.")
def main(
    base_url: str | None,
    api_key_env: str | None,
    reasoning_effort: str | None,
    attempts: int,
    arms: tuple[str, ...],
    cases: tuple[str, ...],
    out: Path,
) -> None:
    provider = selected_provider(base_url, api_key_env, reasoning_effort)
    out = out.resolve()
    folder = out.parent

    def relative(name: str) -> str:
        return os.path.relpath(EXAMPLES / name, folder)

    suite_arms = []
    for name in arms or ARMS:
        image, base, required = ARMS[name]
        config: JsonObject = {
            **base,
            "evidence": EVIDENCE,
            "options": {
                **base["options"],
                "provider": provider_record(provider),
                "model": MODELS.get(name, "openai/gpt-6-luna"),
                **RATES,
            },
        }
        agent = harness_agent(
            config, folder, image, provider, required=required, timeout_seconds=300
        )
        suite_arms.append({"id": name, "agent": agent})
    suite_cases = []
    for name, scenario, overrides, variation in CASES:
        if cases and name not in cases:
            continue
        case: JsonObject = {"id": name, "scenario": relative(scenario)}
        if overrides is not None:
            case["overrides"] = relative(overrides)
        if variation is not None:
            case["variation"] = relative(variation)
        suite_cases.append(case)
    suite = {
        "id": "open-harnesses",
        "attempts": attempts,
        "retries": 1,
        "operator_model": {
            "provider": provider_record(provider),
            "model": "openai/gpt-6-luna",
            **RATES,
        },
        "arms": suite_arms,
        "cases": suite_cases,
    }
    folder.mkdir(parents=True, exist_ok=True)
    with out.open("x") as output:
        json.dump(suite, output, indent=2)
        output.write("\n")
    click.echo(str(out))


if __name__ == "__main__":
    main()
