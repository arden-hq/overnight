from dataclasses import dataclass
from threading import Condition
from typing import Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from overnight.domain.contracts import (
    Action,
    AgentContainer,
    Contract,
    DoneAction,
    HarnessRequirements,
    JsonObject,
    Notification,
)
from overnight.execution.adapters import (
    NO_TRAINING,
    AdapterError,
    AgentBehaviorError,
    ContractError,
    PersistentCommandAdapter,
    Training,
)

notifications_contract = TypeAdapter(list[Notification])


class Capabilities(HarnessRequirements):
    simulated_clock: bool = False
    chat_isolation: bool = False
    cancel: bool = False
    steering: bool = False

    def require(self, required: HarnessRequirements) -> None:
        for name in ("simulated_clock", "chat_isolation", "cancel", "steering"):
            if getattr(required, name) and not getattr(self, name):
                raise ContractError(f"harness lacks required capability: {name}")
        if required.reset_scope != "none" and self.reset_scope != required.reset_scope:
            raise ContractError(f"harness lacks required reset scope: {required.reset_scope}")


class Ready(Contract):
    kind: Literal["ready"]
    version: Literal[1]
    capabilities: Capabilities


class HarnessStatus(Contract):
    pending: bool
    busy: bool = False
    next_wake: int | None = None
    delivered: list[str] = Field(default_factory=list)


class ActionReply(Contract):
    kind: Literal["action"]
    action: Action
    status: HarnessStatus


class LifecycleReply(Contract):
    kind: Literal["reset", "cancel", "close"]
    scope: Literal["none", "process", "chat", "external"] = "none"


class SteerAck(Contract):
    kind: Literal["steer_ack"]
    request_id: str
    session_id: str
    turn_id: str
    accepted: bool
    reason: (
        Literal["inactive", "target_mismatch", "duplicate", "unsupported", "native_rejected"] | None
    ) = None


@dataclass(frozen=True)
class SteeringTarget:
    session_id: str
    turn_id: str


class HarnessAdapter(PersistentCommandAdapter):
    def __init__(
        self,
        command,
        timeout_seconds,
        provenance_path,
        required: HarnessRequirements,
        container: AgentContainer | None = None,
        training: Training = NO_TRAINING,
    ):
        super().__init__(command, timeout_seconds, provenance_path, container, training)
        self.required = required
        self.capabilities: Capabilities | None = None
        self.status = HarnessStatus(pending=False)
        self.chat_id = "default"
        self.session_id = uuid4().hex
        self.active_turn: SteeringTarget | None = None
        self._active_turn_changed = Condition()

    def bind_chat(self, chat_id: str) -> None:
        if self.process_chat_id is not None and self.process_chat_id != chat_id:
            raise AdapterError("a harness adapter cannot serve different episode chats")
        if self.process is not None and self.process_chat_id != chat_id:
            raise AdapterError("cannot bind an episode chat after harness startup")
        self.process_chat_id = chat_id
        self.chat_id = chat_id

    def _start_before_the_agent_decides_anything(self) -> JsonObject:
        try:
            return super().respond(
                {
                    "kind": "hello",
                    "version": 1,
                    "required": self.required.model_dump(mode="json"),
                }
            )
        except AgentBehaviorError as error:
            raise AdapterError(f"harness ended before it was ready: {error}") from error

    def _handshake(self) -> Capabilities:
        if self.capabilities is None:
            reply = self._start_before_the_agent_decides_anything()
            try:
                ready = Ready.model_validate(reply)
                ready.capabilities.require(self.required)
            except ValueError as error:
                raise ContractError(f"invalid harness handshake: {error}") from error
            self.capabilities = ready.capabilities
        return self.capabilities

    def respond(self, observation: JsonObject) -> JsonObject:
        capabilities = self._handshake()
        request: JsonObject = {
            "kind": "observe",
            "chat_id": self.chat_id,
            "observation": observation,
        }
        if capabilities.steering:
            target = SteeringTarget(self.session_id, uuid4().hex)
            request |= {
                "request_id": target.turn_id,
                "message_id": target.turn_id,
                "session_id": target.session_id,
                "turn_id": target.turn_id,
            }

            def publish_target() -> None:
                with self._active_turn_changed:
                    self.active_turn = target
                    self._active_turn_changed.notify_all()

        try:
            reply = self._request_response(
                request,
                require_exit=False,
                response_id=target.turn_id if capabilities.steering else None,
                on_sent=publish_target if capabilities.steering else None,
            )
        finally:
            with self._active_turn_changed:
                self.active_turn = None
                self._active_turn_changed.notify_all()
        try:
            action = ActionReply.model_validate(reply)
        except ValueError as error:
            raise AgentBehaviorError(f"invalid harness action reply: {error}") from error
        self.status = action.status
        return action.action.model_dump(mode="json", exclude_unset=True)

    def wait_for_active_turn(self, timeout_seconds: float | None = None) -> SteeringTarget:
        with self._active_turn_changed:
            if self.active_turn is None:
                self._active_turn_changed.wait_for(
                    lambda: self.active_turn is not None, timeout=timeout_seconds
                )
            if self.active_turn is None:
                raise AdapterError("harness has no active turn")
            return self.active_turn

    def steer(
        self, target: SteeringTarget, message: str, *, request_id: str | None = None
    ) -> SteerAck:
        if not self._handshake().steering:
            raise AdapterError("harness does not support active-turn steering")
        request_id = request_id or uuid4().hex
        reply = self._request_response(
            {
                "kind": "steer",
                "request_id": request_id,
                "chat_id": self.chat_id,
                "session_id": target.session_id,
                "turn_id": target.turn_id,
                "message": message,
                "message_id": request_id,
            },
            require_exit=False,
            response_id=request_id,
        )
        try:
            ack = SteerAck.model_validate(reply)
        except ValueError as error:
            raise AgentBehaviorError(f"invalid steering acknowledgement: {error}") from error
        if (ack.request_id, ack.session_id, ack.turn_id) != (
            request_id,
            target.session_id,
            target.turn_id,
        ):
            raise AgentBehaviorError("steering acknowledgement target does not match request")
        return ack

    def _lifecycle(self, kind: Literal["reset", "cancel", "close"], scope="none") -> None:
        target = self.active_turn
        reply = LifecycleReply.model_validate(
            super().respond({"kind": kind, "chat_id": self.chat_id, "scope": scope})
        )
        if reply.kind != kind or reply.scope != scope:
            raise AgentBehaviorError(f"harness did not acknowledge {kind} scope {scope}")
        if target is not None and self._portal is not None:
            self._portal.call(
                self._fail_response,
                target.turn_id,
                f"active harness turn ended by {kind}",
            )
            with self._active_turn_changed:
                if self.active_turn == target:
                    self.active_turn = None
                    self._active_turn_changed.notify_all()

    def reset(self) -> None:
        capabilities = self._handshake()
        if capabilities.reset_scope not in {"chat", "external"}:
            raise AdapterError("harness cannot reset native chat memory")
        self._lifecycle("reset", capabilities.reset_scope)
        self.status = HarnessStatus(pending=False)
        self.session_id = uuid4().hex

    def cancel(self) -> None:
        if not self._handshake().cancel:
            raise AdapterError("harness cannot cancel")
        self._lifecycle("cancel")
        self.status = HarnessStatus(pending=False)

    def close(self) -> None:
        try:
            if self.capabilities is not None and not self._closed and not self._failed:
                self._lifecycle("close")
        finally:
            super().close()


def prepare_harness(adapter, *, requires_reset: bool = False) -> None:
    if isinstance(adapter, HarnessAdapter):
        capabilities = adapter._handshake()
        if requires_reset and capabilities.reset_scope not in {"chat", "external"}:
            raise ContractError("episode requires native chat memory reset")


def harness_wake(adapter, now: int) -> int | None:
    if not isinstance(adapter, HarnessAdapter):
        return None
    wake = adapter.status.next_wake
    if wake is not None and wake <= now:
        raise AgentBehaviorError("harness next wake must be in the future")
    return wake


def harness_pending(adapter) -> bool:
    return isinstance(adapter, HarnessAdapter) and adapter.status.pending


def harness_busy(adapter) -> bool:
    return isinstance(adapter, HarnessAdapter) and adapter.status.busy


def delivered_ids(adapter, observation: JsonObject, action: Action) -> list[str]:
    offered = [
        notification.id
        for notification in notifications_contract.validate_python(observation["notifications"])
    ]
    if not isinstance(adapter, HarnessAdapter) or isinstance(action, DoneAction):
        return offered
    claimed = adapter.status.delivered
    unknown = sorted(set(claimed) - set(offered))
    if unknown:
        raise AgentBehaviorError(f"harness delivered notifications it was not offered: {unknown}")
    return claimed
