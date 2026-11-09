from pydantic import BaseModel, ConfigDict, Field

from overnight.adapters.config import NativeTurn

NAMESPACE = "ai.primeintellect.prime-agent"


class Quiescence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    outstanding_subagents: int = Field(ge=0, alias="outstandingSubagents")
    remaining_continuation_slots: int = Field(ge=0, alias="remainingAutonomousContinuations")


class Lifecycle(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_turn_id: int = Field(gt=0, alias="promptTurnId")
    event_sequence: int = Field(ge=0, alias="eventSequence")
    phase: str
    outcome: str | None = None
    quiescence: Quiescence | None = None


def validate_completion(turn: NativeTurn) -> None:
    events = []
    for metadata in turn.update_metadata:
        event = metadata.get(NAMESPACE)
        if not isinstance(event, dict):
            continue
        if event.get("phase") in ("responseBoundary", "terminalQuiescence"):
            events.append(Lifecycle.model_validate(event))
    if any(event.outcome == "error" for event in events):
        raise RuntimeError("Prime reported a native runtime error")
    if len(events) < 2:
        raise RuntimeError("Prime completion lacks native boundary/quiescence evidence")
    boundary, terminal = events[-2:]
    if (
        boundary.phase != "responseBoundary"
        or terminal.phase != "terminalQuiescence"
        or boundary.prompt_turn_id != terminal.prompt_turn_id
        or boundary.event_sequence >= terminal.event_sequence
        or boundary.outcome != "result"
        or terminal.outcome != "result"
        or terminal.quiescence is None
    ):
        raise RuntimeError("Prime completion lifecycle does not match the native prompt")
    if terminal.quiescence.outstanding_subagents != 0:
        raise RuntimeError("Prime still has unresolved native work")
