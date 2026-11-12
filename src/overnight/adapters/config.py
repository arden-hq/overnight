import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field

from overnight.adapters.acp_session import NativeClient
from overnight.adapters.protocol import Steer
from overnight.domain.contracts import Contract, JsonObject, TrimmedText
from overnight.execution.harness import Capabilities, HarnessStatus
from overnight.execution.provenance import CodeIdentity, ModelConfiguration
from overnight.execution.tool_server import ToolCatalog


class AdapterConfig(Contract):
    factory: TrimmedText
    evidence: Path
    options: JsonObject = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120, gt=0, le=600)


class NativeTurn(Contract):
    stop_reason: str
    response_metadata: JsonObject = Field(default_factory=dict)
    update_metadata: list[JsonObject] = Field(default_factory=list)


class NativeRequest(Contract):
    method: TrimmedText
    params: JsonObject


class NativeObservation(Contract):
    """Adapter-owned native work, expressed in the existing runner lifecycle."""

    status: HarnessStatus = Field(default_factory=lambda: HarnessStatus(pending=False))
    continuation: str | None = None
    request: NativeRequest | None = None


@dataclass(frozen=True)
class SessionBinding:
    connection: object
    session_id: str
    client: NativeClient | None = None


class Launch(Contract):
    instructions: str = ""
    on_session: Callable[[SessionBinding], Awaitable[None]] | None = Field(
        default=None, exclude=True, repr=False
    )
    observe: Callable[[JsonObject], Awaitable[NativeObservation]] | None = Field(
        default=None, exclude=True, repr=False
    )
    after_turn: Callable[[NativeTurn], Awaitable[NativeObservation]] | None = Field(
        default=None, exclude=True, repr=False
    )
    wait_for_activity: Callable[[], Awaitable[None]] | None = Field(
        default=None, exclude=True, repr=False
    )
    steer: Callable[[Steer], Awaitable[None]] | None = Field(default=None, exclude=True, repr=False)
    cancel: Callable[[], Awaitable[None]] | None = Field(default=None, exclude=True, repr=False)
    reset: Callable[[], Awaitable[None]] | None = Field(default=None, exclude=True, repr=False)
    command: list[TrimmedText] = Field(min_length=1)
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    adapter_code: CodeIdentity
    runtime_code: CodeIdentity | None = None
    model: ModelConfiguration | None = None
    mcp_servers: list[JsonObject] = Field(default_factory=list, repr=False)
    session_metadata: JsonObject = Field(default_factory=dict, repr=False)
    identity_source: Literal["native", "adapter"] = "native"
    meters_usage: bool = False
    validate_turn: Callable[[NativeTurn], None] | None = Field(
        default=None, exclude=True, repr=False
    )
    close: Callable[[], Awaitable[None]] | None = Field(default=None, exclude=True, repr=False)


@dataclass(frozen=True)
class SessionContext:
    chat_id: str
    directory: Path
    operations: ToolCatalog
    endpoint: str
    token: str = field(repr=False)
    options: JsonObject


type Prepare = Callable[[SessionContext], Awaitable[Launch]]


class Factory(Contract):
    """External entry point: capability declaration and native implementation together."""

    capabilities: Capabilities
    client_type: type[NativeClient] = NativeClient
    prepare: Prepare = Field(exclude=True, repr=False)


def load_factory(path: str) -> Factory:
    module_name, separator, name = path.partition(":")
    if not separator or not module_name or not name:
        raise ValueError("factory must be an import path: module:entrypoint")
    factory = getattr(importlib.import_module(module_name), name)
    if not isinstance(factory, Factory):
        raise ValueError("adapter entry point must export a Factory")
    return factory
