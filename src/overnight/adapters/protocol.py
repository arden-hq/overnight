from typing import Literal

from pydantic import TypeAdapter

from overnight.domain.contracts import Contract, HarnessRequirements, JsonObject, TrimmedText


class Hello(Contract):
    kind: Literal["hello"]
    version: Literal[1]
    required: HarnessRequirements


class Observe(Contract):
    kind: Literal["observe"]
    chat_id: str
    observation: JsonObject
    request_id: TrimmedText | None = None
    session_id: TrimmedText | None = None
    turn_id: TrimmedText | None = None
    message_id: TrimmedText | None = None


class Steer(Contract):
    kind: Literal["steer"]
    request_id: TrimmedText
    chat_id: TrimmedText
    session_id: TrimmedText
    turn_id: TrimmedText
    message: TrimmedText
    message_id: TrimmedText


class Lifecycle(Contract):
    kind: Literal["cancel", "reset", "close"]
    chat_id: str
    scope: Literal["none", "process", "chat", "external"] = "none"


request_contract = TypeAdapter(Hello | Observe | Steer | Lifecycle)
