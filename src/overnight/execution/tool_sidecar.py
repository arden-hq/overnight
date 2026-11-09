import sys
import uuid
from typing import Literal

import anyio
from pydantic import Field, TypeAdapter

from overnight.domain.contracts import Contract, JsonObject
from overnight.execution.tool_server import ToolCatalog, TrustedToolContext, WorldToolServer


class Setup(Contract):
    kind: Literal["setup"]
    operations: ToolCatalog
    identity_required: bool = True


class Result(Contract):
    kind: Literal["result"]
    id: str
    result: JsonObject


class Call(Contract):
    kind: Literal["invoke"] = "invoke"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    service: str
    operation: str
    arguments: JsonObject
    context: TrustedToolContext | None


def emit(value: Contract | JsonObject) -> None:
    encoded = (
        value.model_dump_json()
        if isinstance(value, Contract)
        else TypeAdapter(JsonObject).dump_json(value).decode()
    )
    print(encoded, flush=True)


class Invocations:
    def __init__(self) -> None:
        self.pending: dict[str, tuple[anyio.Event, list[JsonObject]]] = {}
        self.abandoned: set[str] = set()

    async def invoke(self, call: Call) -> JsonObject:
        event, result = anyio.Event(), []
        self.pending[call.id] = event, result
        try:
            emit(call)
            await event.wait()
            return result[0]
        finally:
            del self.pending[call.id]
            if not result:
                self.abandoned.add(call.id)

    def deliver(self, response: Result) -> None:
        if response.id in self.abandoned:
            self.abandoned.discard(response.id)
            return
        if response.id not in self.pending:
            raise ValueError("result does not name a pending invocation")
        event, result = self.pending[response.id]
        if result:
            raise ValueError("duplicate invocation result")
        result.append(response.result)
        event.set()


async def main() -> None:
    setup = Setup.model_validate_json(await anyio.to_thread.run_sync(sys.stdin.readline))
    invocations = Invocations()

    async def invoke(service, operation, arguments, context):
        return await invocations.invoke(
            Call(service=service, operation=operation, arguments=arguments, context=context)
        )

    async def read_results(group):
        while line := await anyio.to_thread.run_sync(sys.stdin.readline, abandon_on_cancel=True):
            invocations.deliver(Result.model_validate_json(line))
        group.cancel_scope.cancel()

    server = WorldToolServer(setup.operations, invoke, identity_required=setup.identity_required)
    async with server.serve() as endpoint:
        emit({"kind": "ready", "endpoint": endpoint, "token": server.token})
        async with anyio.create_task_group() as group:
            group.start_soon(read_results, group)


if __name__ == "__main__":
    anyio.run(main)
