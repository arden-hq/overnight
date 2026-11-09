from collections.abc import Callable
from typing import Any

from acp import Client, RequestError
from acp.connection import StreamObserver
from acp.schema import (
    AgentMessageChunk,
    ClientCapabilities,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    UsageUpdate,
)

from overnight.domain.contracts import JsonObject
from overnight.domain.values import json_object


async def refuse_capability(*args, **kwargs) -> Any:
    raise RequestError.method_not_found(
        "Client filesystem, terminal and extension access is disabled"
    )


class NativeClient(Client):
    write_text_file = refuse_capability
    read_text_file = refuse_capability
    create_terminal = refuse_capability
    terminal_output = refuse_capability
    release_terminal = refuse_capability
    wait_for_terminal_exit = refuse_capability
    kill_terminal = refuse_capability
    create_elicitation = refuse_capability
    complete_elicitation = refuse_capability
    ext_method = refuse_capability
    ext_notification = refuse_capability

    def __init__(self, record: Callable[[JsonObject], None], options: JsonObject | None = None):
        self.record = record
        self.reply: list[str] = []
        self.session_id: str | None = None
        self.metadata: list[JsonObject] = []
        self.on_session_cost: Callable[[str, float], None] | None = None

    def capabilities(self) -> ClientCapabilities:
        """External subclasses advertise only the native callbacks they implement."""
        return ClientCapabilities()

    def stream_observers(self) -> list[StreamObserver]:
        """Optional public SDK observers; native event interpretation stays external."""
        return []

    def on_connect(self, conn) -> None:
        pass

    async def request_permission(
        self, session_id: str, tool_call, options: list[PermissionOption], **kwargs
    ) -> RequestPermissionResponse:
        self.record(
            {
                "kind": "acp_permission_refused",
                "session_id": session_id,
                "tool_call": tool_call.model_dump(mode="json", by_alias=True),
            }
        )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        if self.session_id is not None and session_id != self.session_id:
            raise ValueError("ACP update belongs to another session")
        if isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
            self.reply.append(update.content.text)
        if isinstance(update, UsageUpdate) and update.cost is not None:
            if update.cost.currency == "USD" and self.on_session_cost is not None:
                self.on_session_cost(session_id, update.cost.amount)
        metadata = json_object(kwargs)
        if isinstance(getattr(update, "field_meta", None), dict):
            metadata.update(json_object(update.field_meta))
        if metadata:
            self.metadata.append(metadata)
        self.record(
            {
                "kind": "acp_update",
                "session_id": session_id,
                "update": update.model_dump(mode="json", by_alias=True),
                "metadata": metadata,
            }
        )
