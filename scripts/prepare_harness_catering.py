import json
import sys
from pathlib import Path

import click

from overnight.domain.contracts import HarnessAgent
from overnight.execution.scenario import load_json, resolve_scenario


@click.command()
@click.option("--harness", type=click.Choice(["arden", "feather"]), required=True)
@click.option("--run-dir", type=click.Path(path_type=Path), required=True)
@click.option("--out", type=click.Path(path_type=Path), required=True)
@click.option("--max-cost", type=click.FloatRange(min=0, max=5, min_open=True), default=None)
def prepare(harness: str, run_dir: Path, out: Path, max_cost: float | None) -> None:
    if harness == "arden" and max_cost is not None:
        raise click.UsageError(
            "--max-cost is not supported for arden: its connector has no cost cap"
        )
    root = Path(__file__).resolve().parents[1]
    payload = load_json(root / "examples/catering-harness.json")
    command = ["node", str(root / "connectors/arden/host.mjs")]
    if harness == "feather":
        command = [
            sys.executable,
            str(root / "connectors/feather/agent.py"),
            "--model",
            "openai/gpt-6-luna",
            "--input-usd-per-million",
            "0.2",
            "--output-usd-per-million",
            "0.75",
            "--evidence",
            str(run_dir.resolve() / "feather-native"),
            "--max-cost",
            str(1.5 if max_cost is None else max_cost),
        ]
    payload["id"] = f"{harness}-harness-catering"
    payload["agent"] = HarnessAgent(
        kind="harness",
        command=command,
        context={"task_id": "catering", "chat_id": "main"},
        run_on_host=True,
    ).model_dump(mode="json")
    resolved = resolve_scenario(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as output:
        out.chmod(0o600)
        json.dump(resolved.to_dict(), output, indent=2, ensure_ascii=False)
        output.write("\n")
    click.echo(str(out.resolve()))


if __name__ == "__main__":
    prepare()
