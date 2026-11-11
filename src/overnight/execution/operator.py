import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from overnight.domain.contracts import (
    ApprovalRequest,
    Contract,
    Decision,
    EpisodeTask,
    OperatorAnswer,
    OperatorBrief,
    OperatorPersona,
    OperatorRequest,
    OperatorStep,
    Reply,
    ReportRequest,
    Scenario,
    ScenarioError,
    ScheduledDecision,
    Text,
)
from overnight.domain.models import json_equal
from overnight.domain.values import JsonObject
from overnight.domain.vocabulary import ApprovalDecision
from overnight.execution.adapters import AdapterError, ContractError
from overnight.execution.usage import NO_CALLS, ProcessUsage
from overnight.services.operations import ServiceBoundary

type Expects = Literal["reply", "decision"]
type StepKey = tuple[int, int]
type Message = dict[str, str]
type Canonical = Callable[[str, str, JsonObject], JsonObject]


class OperatorError(AdapterError):
    def __init__(self, message: str, calls: Sequence["OperatorCall"] = ()) -> None:
        super().__init__(message)
        self.calls = list(calls)


class OperatorUnavailable(OperatorError):
    """The model transport failed or the model gave no valid answer; a rerun may succeed."""


class OperatorBudgetExceeded(OperatorError, ContractError):
    """The run's operator call or cost cap was reached before a needed call."""


class OperatorReplayDiverged(OperatorError, ContractError):
    """A replay asked something the recorded answers do not hold, or left records unused."""


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class OperatorCall(EvidenceModel):
    prompt: list[Message]
    prompt_sha256: str
    raw: str
    usage: ProcessUsage


class AnswerEvidence(EvidenceModel):
    source: Literal["scripted", "model", "recorded", "authored", "qa"]
    prompt_version: str | None = None
    model: str | None = None
    settings: JsonObject | None = None
    calls: list[OperatorCall] = []


@dataclass(frozen=True)
class AnsweredRequest:
    answer: OperatorAnswer
    evidence: AnswerEvidence


@dataclass(frozen=True)
class TranscriptLine:
    time: int
    speaker: Literal["you", "assistant", "approval request", "your decision"]
    text: str


@dataclass(frozen=True)
class OperatorView:
    task_id: str | None
    transcript: Sequence[TranscriptLine]


@dataclass(frozen=True)
class Completion:
    text: str
    usage: ProcessUsage
    model: str | None = None
    settings: JsonObject | None = None


class OperatorTransport(Protocol):
    """Sends one prompt and returns the model's text. ``source`` names where answers come from."""

    @property
    def source(self) -> Literal["model", "recorded", "authored", "qa"]: ...

    def complete(self, prompt: Sequence[Message], expects: Expects) -> Completion: ...

    def finish(self) -> None:
        """Called once after a run that did not fail; a replay checks every record was used."""
        ...


class Operator(Protocol):
    def answer(self, request: OperatorRequest, view: OperatorView) -> AnsweredRequest | None: ...

    def due(
        self, now: int, open: Sequence[ApprovalRequest], before: StepKey | None = None
    ) -> list[AnsweredRequest]: ...

    def next_time(self, now: int) -> int | None: ...

    def next_work_time(self, now: int, open: Sequence[ApprovalRequest]) -> int | None: ...

    def outstanding(self, open: Sequence[ApprovalRequest]) -> list[str]: ...

    def finish(self) -> None: ...

    @property
    def usage(self) -> ProcessUsage: ...

    def provenance(self) -> JsonObject: ...


def canonical_arguments(config: Scenario, definitions: Mapping[str, ServiceBoundary]) -> Canonical:
    kinds = {service.id: service.kind for service in config.services}

    def canonical(service_id: str, operation: str, args: JsonObject) -> JsonObject:
        spec = definitions[kinds[service_id]].operations.get(operation)
        if spec is None:
            raise ValueError(f"unknown operation: {service_id}.{operation}")
        return spec.canonical_args(args)

    return canonical


class ScriptedOperator:
    def __init__(self, steps: Iterable[OperatorStep], canonical: Canonical) -> None:
        indexed = [
            (index, step) for index, step in enumerate(steps) if isinstance(step, ScheduledDecision)
        ]
        self.steps = sorted(indexed, key=lambda item: (item[1].at, item[0]))
        self.completed: set[int] = set()
        self.canonical = canonical
        self.wanted = {
            index: self._read(step.match.service_id, step.match.operation, step.match.args)
            for index, step in indexed
        }
        self.asked: dict[str, JsonObject | None] = {}

    def _read(self, service_id: str, operation: str, args: JsonObject) -> JsonObject | None:
        try:
            return self.canonical(service_id, operation, args)
        except ValueError:
            return None

    def _requested(self, request: ApprovalRequest) -> JsonObject | None:
        if request.id not in self.asked:
            self.asked[request.id] = self._read(request.service_id, request.operation, request.args)
        return self.asked[request.id]

    def _matches(self, index: int, step: ScheduledDecision, request: ApprovalRequest) -> bool:
        if (request.service_id, request.operation) != (step.match.service_id, step.match.operation):
            return False
        wanted, requested = self.wanted[index], self._requested(request)
        return wanted is not None and requested is not None and json_equal(requested, wanted)

    def _open(self) -> list[tuple[int, ScheduledDecision]]:
        return [(index, step) for index, step in self.steps if index not in self.completed]

    def answer(self, request: OperatorRequest, view: OperatorView) -> AnsweredRequest | None:
        return None

    def due(
        self, now: int, open: Sequence[ApprovalRequest], before: StepKey | None = None
    ) -> list[AnsweredRequest]:
        answered: list[AnsweredRequest] = []
        taken: set[str] = set()
        for index, step in self._open():
            if step.at > now or (before is not None and (step.at, index) >= before):
                continue
            request = next(
                (
                    item
                    for item in open
                    if item.id not in taken and self._matches(index, step, item)
                ),
                None,
            )
            if request is None:
                continue
            taken.add(request.id)
            self.completed.add(index)
            answered.append(
                AnsweredRequest(
                    Decision(kind="decision", request_id=request.id, decision=step.kind),
                    AnswerEvidence(source="scripted"),
                )
            )
        return answered

    def next_time(self, now: int) -> int | None:
        return min((step.at for _, step in self._open() if step.at > now), default=None)

    def next_work_time(self, now: int, open: Sequence[ApprovalRequest]) -> int | None:
        return min(
            (
                step.at
                for index, step in self._open()
                if step.at > now and any(self._matches(index, step, request) for request in open)
            ),
            default=None,
        )

    def outstanding(self, open: Sequence[ApprovalRequest]) -> list[str]:
        return [
            step.kind
            for index, step in self._open()
            if any(self._matches(index, step, request) for request in open)
        ]

    def finish(self) -> None:
        pass

    @property
    def usage(self) -> ProcessUsage:
        return NO_CALLS

    def provenance(self) -> JsonObject:
        return {"kind": "scripted"}


SYSTEM_PROMPT = """You are the user described below. You gave an AI assistant one or more tasks, \
each in its own chat.
You see only what the assistant tells you in a chat and the actions it asks you to approve.
Answer as this user, briefly, in your own voice.
Use only your brief and what you are shown. Never state a fact about the world that is not in them.
If your brief does not cover a question, say so in character ("I don't know", "your call") and \
do not invent an answer.
Do not do the assistant's work and do not give it instructions beyond what your brief says you want.
Approve an action only when your brief allows it and what the assistant has told you in this chat \
meets your conditions.
Otherwise deny it and say why, or ask what you need to know.
Stay consistent with what you have already said in this chat: if you told the assistant to go \
ahead with an action, approve that action when it asks.
If a message needs no answer (a status update, thanks, a final summary you are content with), \
reply with null.
Return only JSON: {"message": string|null} for a message, \
{"decision": "approve"|"deny", "message": string|null} for an approval."""

REPLY_FORMAT = 'Answer with JSON only: {"message": string|null}.'
DECISION_FORMAT = 'Answer with JSON only: {"decision": "approve"|"deny", "message": string|null}.'
REASK = "That answer was invalid ({error}). {format}"
PROMPT_VERSION = hashlib.sha256(
    "\0".join((SYSTEM_PROMPT, REPLY_FORMAT, DECISION_FORMAT, REASK, "v1")).encode()
).hexdigest()[:16]


class ReplyOutput(Contract):
    message: Text | None


class DecisionOutput(Contract):
    decision: Literal["approve", "deny"]
    message: Text | None = None


def prompt_digest(prompt: Sequence[Message]) -> str:
    encoded = json.dumps(list(prompt), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _items(label: str, values: Sequence[str]) -> list[str]:
    return [f"  {label}:", *(f"  - {value}" for value in values)] if values else []


def _brief_block(
    brief: OperatorBrief, opening: str | None, label: str, this_chat: bool
) -> list[str]:
    lines = [f"- {label}{' (this chat)' if this_chat else ''}"]
    if opening is not None:
        lines.append(f"  You asked: {opening}")
    lines.append(f"  You want: {brief.wants}")
    lines += _items("You know", brief.knows)
    lines += _items("Your preferences", brief.prefers)
    return lines


def _request_text(request: OperatorRequest) -> str:
    if isinstance(request, ReportRequest):
        return f"[t={request.time}] The assistant says: {request.message}\n{REPLY_FORMAT}"
    return (
        f"[t={request.time}] The assistant asks you to approve running "
        f"{request.service_id}.{request.operation} with {_canonical(request.args)}\n"
        f"{DECISION_FORMAT}"
    )


def _parse(text: str, expects: Expects) -> ReplyOutput | DecisionOutput:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].removeprefix("json").strip()
    contract = ReplyOutput if expects == "reply" else DecisionOutput
    return contract.model_validate(json.loads(stripped))


class PersonaOperator:
    def __init__(
        self,
        persona: OperatorPersona,
        tasks: Sequence[EpisodeTask],
        transport: OperatorTransport,
    ) -> None:
        self.persona = persona
        self.openings = {task.id: task.message for task in tasks}
        self.transport = transport
        self._usage = NO_CALLS
        self.settings: JsonObject | None = None
        self.model_name: str | None = None

    @property
    def usage(self) -> ProcessUsage:
        return self._usage

    def prompt(self, request: OperatorRequest, view: OperatorView) -> list[Message]:
        tasks: list[str] = ["Your tasks:"]
        for brief in self.persona.briefs:
            asking = brief.task_id is None or brief.task_id == view.task_id
            label = "Your task" if brief.task_id is None else f"Task {brief.task_id}"
            opening = self.openings.get(brief.task_id) if brief.task_id is not None else None
            tasks += _brief_block(brief, opening, label, asking)
        chat = ["This chat so far:"]
        chat += [f"[t={line.time}] {line.speaker}: {line.text}" for line in view.transcript]
        if not view.transcript:
            chat.append("(nothing yet)")
        user = "\n".join([*tasks, "", *chat, "", "Now:", _request_text(request)])
        return [
            {
                "role": "system",
                "content": f"{SYSTEM_PROMPT}\n\nWho you are: {self.persona.description}",
            },
            {"role": "user", "content": user},
        ]

    def _complete(
        self, prompt: list[Message], expects: Expects, calls: list[OperatorCall]
    ) -> Completion:
        limits = self.persona.model
        cost = self._usage.cost_usd
        calls_made = self._usage.calls
        if calls_made is None or cost is None:
            raise OperatorUnavailable(
                "operator usage is unmeasured, so max_calls and max_cost_usd cannot be enforced",
                calls,
            )
        if calls_made >= limits.max_calls or cost >= limits.max_cost_usd:
            raise OperatorBudgetExceeded(
                f"operator budget reached: {self._usage.calls} calls, cost {cost} USD "
                f"(limits {limits.max_calls} calls, {limits.max_cost_usd} USD)",
                calls,
            )
        try:
            completion = self.transport.complete(prompt, expects)
        except Exception as error:
            if self.transport.source == "model":
                self._usage = self._usage.plus(ProcessUsage(calls=1))
            if isinstance(error, OperatorError):
                error.calls = [*calls, *error.calls]
                raise
            raise OperatorUnavailable(f"operator transport failed: {error}", calls) from error
        self._usage = self._usage.plus(completion.usage)
        self.settings = completion.settings
        self.model_name = completion.model
        calls.append(
            OperatorCall(
                prompt=prompt,
                prompt_sha256=prompt_digest(prompt),
                raw=completion.text,
                usage=completion.usage,
            )
        )
        return completion

    def answer(self, request: OperatorRequest, view: OperatorView) -> AnsweredRequest:
        expects: Expects = "reply" if isinstance(request, ReportRequest) else "decision"
        prompt = self.prompt(request, view)
        calls: list[OperatorCall] = []
        completion = self._complete(prompt, expects, calls)
        try:
            output = _parse(completion.text, expects)
        except (ValueError, ValidationError) as error:
            reason = _reason(error)
            prompt = [
                *prompt,
                {"role": "assistant", "content": completion.text},
                {
                    "role": "user",
                    "content": REASK.format(
                        error=reason,
                        format=REPLY_FORMAT if expects == "reply" else DECISION_FORMAT,
                    ),
                },
            ]
            completion = self._complete(prompt, expects, calls)
            try:
                output = _parse(completion.text, expects)
            except (ValueError, ValidationError) as second:
                raise OperatorUnavailable(
                    f"operator gave no valid answer to {request.id}: {_reason(second)}", calls
                ) from second
        evidence = AnswerEvidence(
            source=self.transport.source,
            prompt_version=PROMPT_VERSION,
            model=completion.model,
            settings=completion.settings,
            calls=calls,
        )
        if isinstance(output, DecisionOutput):
            answer: OperatorAnswer = Decision(
                kind="decision",
                request_id=request.id,
                decision=ApprovalDecision(output.decision),
                message=output.message,
            )
        else:
            answer = Reply(kind="reply", request_id=request.id, message=output.message)
        return AnsweredRequest(answer, evidence)

    def due(
        self, now: int, open: Sequence[ApprovalRequest], before: StepKey | None = None
    ) -> list[AnsweredRequest]:
        return []

    def next_time(self, now: int) -> int | None:
        return None

    def next_work_time(self, now: int, open: Sequence[ApprovalRequest]) -> int | None:
        return None

    def outstanding(self, open: Sequence[ApprovalRequest]) -> list[str]:
        return []

    def finish(self) -> None:
        self.transport.finish()

    def provenance(self) -> JsonObject:
        model = self.persona.model
        return {
            "kind": "persona",
            "transport": self.transport.source,
            "provider": model.model_dump(mode="json")["provider"],
            "configured_model": model.model,
            "model": self.model_name,
            "settings": self.settings,
            "prompt_version": PROMPT_VERSION,
        }


def _reason(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'answer'}: {item['msg']}"
            for item in error.errors()
        )
    return f"not JSON: {error}"


@dataclass
class RecordedTransport:
    records: list[tuple[str, str]]
    source: Literal["recorded"] = "recorded"
    position: int = field(default=0)

    @classmethod
    def from_entries(cls, entries: Iterable[JsonObject]) -> "RecordedTransport":
        records: list[tuple[str, str]] = []
        for entry in entries:
            payload = entry.get("payload") if entry.get("kind") == "operator_answer" else entry
            if not isinstance(payload, dict) or "evidence" not in payload:
                continue
            evidence = AnswerEvidence.model_validate(payload["evidence"], strict=False)
            records += [(call.prompt_sha256, call.raw) for call in evidence.calls]
        return cls(records)

    @classmethod
    def load(cls, path: Path) -> "RecordedTransport":
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            entries = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError) as error:
            raise ScenarioError(f"cannot read operator answers {path}: {error}") from error
        return cls.from_entries(entry for entry in entries if isinstance(entry, dict))

    def complete(self, prompt: Sequence[Message], expects: Expects) -> Completion:
        digest = prompt_digest(prompt)
        if self.position >= len(self.records):
            raise OperatorReplayDiverged(f"no recorded operator answer for prompt {digest}")
        recorded, raw = self.records[self.position]
        if recorded != digest:
            raise OperatorReplayDiverged(
                f"operator prompt {digest} differs from recorded prompt {recorded} "
                f"at record {self.position}"
            )
        self.position += 1
        return Completion(text=raw, usage=NO_CALLS)

    def finish(self) -> None:
        if self.position != len(self.records):
            raise OperatorReplayDiverged(
                f"{len(self.records) - self.position} recorded operator answers were not used"
            )


class QaTransport:
    source: Literal["qa"] = "qa"

    def complete(self, prompt: Sequence[Message], expects: Expects) -> Completion:
        text = (
            '{"message": null}'
            if expects == "reply"
            else '{"decision": "deny", "message": "QA probe: no operator"}'
        )
        return Completion(text=text, usage=NO_CALLS)

    def finish(self) -> None:
        pass


def make_operator(
    config: Scenario,
    transport: OperatorTransport | None,
    definitions: Mapping[str, ServiceBoundary],
) -> Operator:
    persona = config.operator.persona
    if persona is None:
        if transport is not None:
            raise ScenarioError("operator answers need a scenario with an operator persona")
        return ScriptedOperator(config.operator.steps, canonical_arguments(config, definitions))
    if transport is None:
        raise ScenarioError(
            "this scenario's operator is a persona: pass a model transport or recorded answers"
        )
    return PersonaOperator(persona, config.tasks, transport)
