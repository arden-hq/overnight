import json
from collections.abc import Sequence
from pathlib import Path

import click

from overnight.execution.operator import Completion, Expects, Message
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import load_json, resolve_scenario
from overnight.execution.usage import NO_CALLS

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/shared-world-chaos.json"
ANSWERS = ROOT / "examples/shared-world-chaos-operator.jsonl"


class AuthoredAnswers:
    source = "authored"

    def complete(self, prompt: Sequence[Message], expects: Expects) -> Completion:
        text = prompt[-1]["content"]
        chat, now = text.split("Now:")[0], text.split("Now:")[-1]
        catering = "Task catering (this chat)" in chat
        confirmed = "confirmed order original can be cancelled for a full refund" in text
        if expects == "reply":
            asked = catering and "May I cancel" in now and confirmed
            answer = {"message": "Yes, go ahead and cancel order original." if asked else None}
        elif catering and confirmed and "shop.cancel_order" in now and "original" in now:
            answer = {"decision": "approve", "message": None}
        else:
            answer = {"decision": "deny", "message": "The catering chat is handling that."}
        return Completion(text=json.dumps(answer), usage=NO_CALLS)

    def finish(self) -> None:
        pass


def authored_lines() -> list[str]:
    result = run_scenario(
        resolve_scenario(load_json(SCENARIO)),
        operator_transport=AuthoredAnswers(),
    )
    if result.verdict["verdict"] != "passed":
        raise click.ClickException(f"the oracle did not pass: {result.verdict}")
    return [
        json.dumps(entry["payload"], sort_keys=True, ensure_ascii=False) + "\n"
        for entry in result.trace
        if entry["kind"] == "operator_answer"
    ]


@click.command()
def main() -> None:
    lines = authored_lines()
    ANSWERS.write_text("".join(lines), encoding="utf-8")
    click.echo(f"wrote {len(lines)} answers to {ANSWERS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
