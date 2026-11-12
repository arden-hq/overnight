import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import click

from overnight.domain.contracts import JsonObject
from overnight.execution.operator import Completion, Expects, Message
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import load_json, resolve_scenario
from overnight.execution.usage import NO_CALLS

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

type Answer = Callable[[str, str, Expects], JsonObject]


def shared_world_answer(chat: str, now: str, expects: Expects) -> JsonObject:
    catering = "Task catering (this chat)" in chat
    confirmed = "confirmed order original can be cancelled for a full refund" in chat + now
    if expects == "reply":
        asked = catering and "May I cancel" in now and confirmed
        return {"message": "Yes, go ahead and cancel order original." if asked else None}
    if catering and confirmed and "shop.cancel_order" in now and "original" in now:
        return {"decision": "approve", "message": None}
    return {"decision": "deny", "message": "The catering chat is handling that."}


def offsite_answer(chat: str, now: str, expects: Expects) -> JsonObject:
    if expects == "reply":
        return {"message": None}
    told_price_in_budget = (
        "assistant: " in chat and "240000 cents" in chat.split("assistant: ", 1)[1]
    )
    if told_price_in_budget and 'shop.buy with {"item_id":"loft-fri"}' in now:
        return {"decision": "approve", "message": None}
    return {"decision": "deny", "message": "Only the venue day, once you've told me its price."}


@dataclass(frozen=True)
class Case:
    scenario: Path
    answers: Path
    answer: Answer


CASES = {
    "shared-world-chaos": Case(
        EXAMPLES / "shared-world-chaos.json",
        EXAMPLES / "shared-world-chaos-operator.jsonl",
        shared_world_answer,
    ),
    "offsite-week": Case(
        EXAMPLES / "offsite-week-oracle.json",
        EXAMPLES / "offsite-week-operator.jsonl",
        offsite_answer,
    ),
}


class AuthoredAnswers:
    source = "authored"

    def __init__(self, answer: Answer) -> None:
        self.answer = answer

    def complete(self, prompt: Sequence[Message], expects: Expects) -> Completion:
        chat, _, now = prompt[-1]["content"].rpartition("Now:")
        return Completion(text=json.dumps(self.answer(chat, now, expects)), usage=NO_CALLS)

    def finish(self) -> None:
        pass


def authored_lines(case: Case) -> list[str]:
    result = run_scenario(
        resolve_scenario(load_json(case.scenario)),
        operator_transport=AuthoredAnswers(case.answer),
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
    for case in CASES.values():
        lines = authored_lines(case)
        case.answers.write_text("".join(lines), encoding="utf-8")
        click.echo(f"wrote {len(lines)} answers to {case.answers.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
