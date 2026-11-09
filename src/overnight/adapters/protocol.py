from typing import Literal

from pydantic import TypeAdapter

from overnight.domain.contracts import Contract, HarnessRequirements, JsonObject, Text


class Hello(Contract):
    kind: Literal["hello"]
    version: Literal[1]
    required: HarnessRequirements


class Observe(Contract):
    kind: Literal["observe"]
    chat_id: str
    observation: JsonObject
    request_id: Text | None = None
    session_id: Text | None = None
    turn_id: Text | None = None
    message_id: Text | None = None


class Steer(Contract):
    kind: Literal["steer"]
    request_id: Text
    chat_id: Text
    session_id: Text
    turn_id: Text
    message: Text
    message_id: Text


class Lifecycle(Contract):
    kind: Literal["cancel", "reset", "close"]
    chat_id: str
    scope: Literal["none", "process", "chat", "external"] = "none"


request_contract = TypeAdapter(Hello | Observe | Steer | Lifecycle)
