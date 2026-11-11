import hmac
import json
import secrets
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal, Self

from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

from overnight.adapters.http import serve_http
from overnight.domain.contracts import Contract, Fact, JsonObject, Text

NATIVE_CONTEXT_KEY = "arden/execution"


class TrustedToolContext(Fact):
    """One operation's identity, supplied outside model-authored arguments.

    Direct tools retain their native call ID. Programmatic callers mint a distinct
    ID for each operation and record a trusted native parent when available.
    """

    run_id: Text
    tool_call_id: Text
    chat_id: Text
    identity_source: Literal["native", "adapter"] = "native"
    parent_call_id: Text | None = None

    @classmethod
    def for_operation(cls, run_id: str, chat_id: str, *, parent_call_id: str | None = None) -> Self:
        """Call from a trusted dispatch wrapper, never from evaluated code."""
        return cls(
            run_id=run_id,
            chat_id=chat_id,
            tool_call_id=f"operation-{uuid.uuid4().hex}",
            identity_source="adapter",
            parent_call_id=parent_call_id,
        )


class ToolOperation(Contract):
    name: Text
    description: str = ""
    input_schema: JsonObject
    read_only: bool = False


type ToolCatalog = dict[str, list[ToolOperation]]
type Invoke = Callable[[str, str, JsonObject, TrustedToolContext | None], Awaitable[JsonObject]]


class WorldToolServer:
    def __init__(self, operations: ToolCatalog, invoke: Invoke, *, identity_required: bool = False):
        self.token = secrets.token_urlsafe(32)
        self.operations = operations
        self.invoke = invoke
        self.identity_required = identity_required

    def _service(self, service: str, operations: list[ToolOperation]) -> Server:
        server = Server(service)
        catalog = {operation.name: operation for operation in operations}

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=op.name,
                    description=op.description,
                    inputSchema=op.input_schema,
                    _meta={NATIVE_CONTEXT_KEY: True} if self.identity_required else None,
                    annotations=types.ToolAnnotations(readOnlyHint=op.read_only),
                )
                for op in operations
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: JsonObject) -> types.CallToolResult:
            if name not in catalog:
                raise ValueError("unknown operation")
            meta = server.request_context.meta
            raw = None if meta is None else meta.model_dump().get(NATIVE_CONTEXT_KEY)
            context = None if raw is None else TrustedToolContext.model_validate(raw)
            if self.identity_required and context is None:
                raise ValueError("trusted invocation identity is required")
            result = await self.invoke(service, name, arguments, context)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                isError=result.get("status") != "succeeded",
            )

        return server

    @asynccontextmanager
    async def serve(self) -> AsyncGenerator[str]:
        async with AsyncExitStack() as stack:
            routes = []
            for service, operations in self.operations.items():
                manager = StreamableHTTPSessionManager(
                    self._service(service, operations), json_response=True, stateless=True
                )
                await stack.enter_async_context(manager.run())

                routes.append(
                    Route(
                        f"/{service}",
                        _Endpoint(manager, self.token),
                        methods=["GET", "POST", "DELETE"],
                    )
                )
            app = Starlette(routes=routes)
            async with serve_http(app) as endpoint:
                yield endpoint


class _Endpoint:
    def __init__(self, manager: StreamableHTTPSessionManager, token: str):
        self.manager = manager
        self.authorization = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self.authorization):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self.manager.handle_request(scope, receive, send)
