import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import click
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.core import ClientSideConnection
from acp.schema import (
    AcpMcpServer,
    HttpMcpServer,
    McpServerStdio,
    PromptResponse,
    SseMcpServer,
)
from pydantic import TypeAdapter

from overnight.adapters.bridge import ToolBridge, catalog_contract, objects_contract
from overnight.adapters.config import (
    AdapterConfig,
    Factory,
    Launch,
    NativeObservation,
    NativeTurn,
    SessionBinding,
    SessionContext,
    load_factory,
)
from overnight.adapters.evidence import UsageMeter, write_provenance
from overnight.adapters.host import serve
from overnight.adapters.protocol import Lifecycle, Observe, Steer
from overnight.domain.contracts import JsonObject, action_contract
from overnight.domain.values import json_object, json_value
from overnight.execution.container import network_environment
from overnight.execution.harness import ActionReply, Capabilities, HarnessStatus, LifecycleReply
from overnight.execution.tool_server import TrustedToolContext, WorldToolServer

INSTRUCTIONS = """World services are reached only through the supplied world tools. Observations give
the current world time. Operator messages and notifications (new mail, chat messages, changes made
by others, due reminders) arrive as new turns of the same conversation, listed under
"notifications". Some operations need operator approval: calling one asks the operator and waits
for the decision. The system tools are always available: current_time reads the world time,
wait_for_notification waits until a notification for you arrives or a timeout passes, and
set_reminder, list_reminders and cancel_reminder schedule follow-ups for yourself. After you end a
turn you are idle, and only a notification starts your next turn."""


def validate_launch(capabilities: Capabilities, launch: Launch) -> None:
    if capabilities.steering and launch.steer is None:
        raise ValueError("steering capability requires a native steer hook")
    if capabilities.simulated_clock and launch.observe is None:
        raise ValueError("simulated_clock capability requires a native observe hook")


class Connector:
    def __init__(self, config: AdapterConfig, factory: Factory):
        if factory.capabilities.reset_scope not in ("none", "chat"):
            raise ValueError("ACP connector implements reset scope none or chat only")
        self.config = config
        self.factory = factory
        self.prepare = factory.prepare
        self.native = NativeObservation()
        self.directory = config.evidence.resolve()
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.log = self.directory / "native.jsonl"
        self.log.touch(mode=0o600)
        self.stack = AsyncExitStack()
        self.client = factory.client_type(self.record, config.options)
        self.meter = UsageMeter()
        self.client.on_session_cost = self._record_session_cost
        self.connection: ClientSideConnection | None = None
        self.bridge: ToolBridge | None = None
        self.turn: asyncio.Task | None = None
        self.metered_turn: asyncio.Task | None = None
        self.chat_id: str | None = None
        self.delivered_notifications: set[str] = set()
        self.session_prompted = False
        self.turn_delivered: list[str] = []
        self.closed = False
        self.launch: Launch | None = None
        self.resource_task: asyncio.Task[None] | None = None
        self.resources_ready = asyncio.Event()
        self.resources_stop = asyncio.Event()
        self.resource_error: BaseException | None = None

    def record(self, value: JsonObject) -> None:
        with self.log.open("a", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")

    async def _start_resources(self, message: Observe) -> None:
        self.chat_id = message.chat_id
        self.bridge = ToolBridge(message.chat_id, self.record)
        operations = catalog_contract.validate_python(message.observation["operations"])

        async def invoke(service, operation, arguments, identity):
            assert self.bridge is not None
            if self.launch is not None and self.launch.identity_source == "adapter":
                if self.client.session_id is None:
                    raise ValueError("native session is not bound")
                identity = TrustedToolContext.for_operation(self.client.session_id, message.chat_id)
            return await self.bridge.invoke(service, operation, arguments, identity)

        server = WorldToolServer(operations, invoke, identity_required=True)
        endpoint = await self.stack.enter_async_context(server.serve())
        launch = await self.prepare(
            SessionContext(
                message.chat_id,
                self.directory,
                operations,
                endpoint,
                server.token,
                self.config.options,
            )
        )
        self.launch = launch
        if launch.close is not None:
            self.stack.push_async_callback(launch.close)
        validate_launch(self.factory.capabilities, launch)
        server.identity_required = launch.identity_source == "native"
        write_provenance(self.config.factory, launch)
        stderr = self.stack.enter_context((self.directory / "stderr.log").open("xb"))
        os.chmod(self.directory / "stderr.log", 0o600)
        self.connection, process = await self.stack.enter_async_context(
            spawn_agent_process(
                self.client,
                *launch.command,
                env={**network_environment(), **launch.env},
                cwd=launch.cwd,
                transport_kwargs={"stderr": stderr, "shutdown_timeout": 2},
                observers=self.client.stream_observers(),
            )
        )
        initialized = await self.connection.initialize(
            protocol_version=PROTOCOL_VERSION, client_capabilities=self.client.capabilities()
        )
        if initialized.protocol_version != PROTOCOL_VERSION:
            raise ValueError("ACP agent negotiated an unsupported protocol version")
        await self.new_session()
        self.record(
            {
                "kind": "acp_ready",
                "session_id": self.client.session_id,
                "pid": process.pid,
                "agent": initialized.agent_info.model_dump(mode="json")
                if initialized.agent_info
                else None,
                "chat_id": message.chat_id,
                "capabilities": self.factory.capabilities.model_dump(mode="json"),
            }
        )

    async def _own_resources(self, message: Observe) -> None:
        try:
            await self._start_resources(message)
        except BaseException as error:
            self.resource_error = error
        finally:
            self.resources_ready.set()
        try:
            if self.resource_error is None:
                await self.resources_stop.wait()
        finally:
            await self.stack.aclose()

    async def start(self, message: Observe) -> None:
        if self.resource_task is None:
            self.resource_task = asyncio.create_task(self._own_resources(message))
        await self.resources_ready.wait()
        if self.resource_error is not None:
            raise self.resource_error

    async def new_session(self) -> None:
        assert self.connection is not None and self.launch is not None
        session = await self.connection.new_session(
            cwd=str(self.launch.cwd),
            additional_directories=None,
            mcp_servers=TypeAdapter(
                list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
            ).validate_python(self.launch.mcp_servers),
            **self.launch.session_metadata,
        )
        self.client.session_id = session.session_id
        self.session_prompted = False
        assert self.bridge is not None
        self.bridge.run_id = session.session_id
        if self.launch.on_session is not None:
            await self.launch.on_session(
                SessionBinding(self.connection, session.session_id, self.client)
            )

    async def observe(self, message: Observe) -> ActionReply:
        if self.closed:
            raise ValueError("connector is closed")
        async with asyncio.timeout(self.config.timeout_seconds):
            if self.connection is None:
                await self.start(message)
            if message.chat_id != self.chat_id:
                raise ValueError("ACP connector cannot switch chats")
            return await self.advance(message.observation)

    async def steer(self, message: Steer) -> None:
        if self.launch is None or self.launch.steer is None:
            raise ValueError("native turn does not accept steering")
        if self.turn is None or self.turn.done():
            raise ValueError("no active native turn to steer")
        async with asyncio.timeout(self.config.timeout_seconds):
            await self.launch.steer(message)

    async def advance(self, observation: JsonObject) -> ActionReply:
        assert self.bridge is not None
        if self.launch is not None and self.launch.observe is not None:
            self.native = await self.launch.observe(observation)
        if self.bridge.pending is not None:
            retry = self.bridge.resolve(observation["receipts"])
            if retry is not None:
                return self.reply(retry)
        while self.turn is None:
            new = self.new_notifications(observation)
            if self.native.request is not None:
                self.start_native_request()
            elif new or self.native.continuation is not None:
                self.start_turn(observation, new)
            else:
                action = await self.idle_action(observation)
                if action is not None:
                    return self.reply(action)
        return await self.run_turn(observation)

    async def run_turn(self, observation: JsonObject) -> ActionReply:
        """Wait for the native turn's next world call or its end. A turn that ends silently is
        followed at once by a turn for notifications that arrived meanwhile."""
        assert self.bridge is not None
        while True:
            assert self.turn is not None
            action = asyncio.create_task(self.bridge.next_action())
            try:
                ready, _ = await asyncio.wait(
                    (action, self.turn), return_when=asyncio.FIRST_COMPLETED
                )
                if action in ready:
                    return self.reply(action.result())
                action.cancel()
                await asyncio.gather(action, return_exceptions=True)
                report = await self.finish_turn()
            finally:
                if not action.done():
                    action.cancel()
                    await asyncio.gather(action, return_exceptions=True)
            if report is not None:
                return self.reply(report)
            new = self.new_notifications(observation)
            if not new:
                return self.reply({"kind": "done"})
            self.start_turn(observation, new)

    def new_notifications(self, observation: JsonObject) -> list[JsonObject]:
        """The observation's notifications that no native prompt has carried yet."""
        return [
            notification
            for notification in objects_contract.validate_python(observation["notifications"])
            if notification["id"] not in self.delivered_notifications
        ]

    async def idle_action(self, observation: JsonObject) -> JsonObject | None:
        assert self.bridge is not None
        if self.bridge.has_pending:
            return await self.bridge.next_action()
        if not self.native.status.busy:
            wake = self.native.status.next_wake
            horizon = observation.get("horizon")
            if wake is not None and (not isinstance(horizon, int) or wake <= horizon):
                return {"kind": "wait", "until": wake}
            return {"kind": "wait"} if self.native.status.pending else {"kind": "done"}
        if (
            self.launch is None
            or self.launch.wait_for_activity is None
            or self.launch.observe is None
        ):
            raise RuntimeError("runnable native background work requires an activity hook")
        action = asyncio.create_task(self.bridge.next_action())
        activity = asyncio.ensure_future(self.launch.wait_for_activity())
        try:
            ready, _ = await asyncio.wait((action, activity), return_when=asyncio.FIRST_COMPLETED)
            if action not in ready:
                await activity
                action.cancel()
                await asyncio.gather(action, return_exceptions=True)
            if not action.cancelled():
                return action.result()
            self.native = await self.launch.observe(observation)
            return None
        finally:
            for task in (action, activity):
                if not task.done():
                    task.cancel()
            await asyncio.gather(action, activity, return_exceptions=True)

    def start_native_request(self) -> None:
        assert self.native.request is not None and self.connection is not None
        request = self.native.request
        self.client.reply.clear()
        self.client.metadata.clear()
        self.record(
            {
                "kind": "acp_native_request",
                "session_id": self.client.session_id,
                "method": request.method,
            }
        )

        async def invoke():
            assert self.connection is not None
            result = await self.connection.ext_method(request.method, request.params)
            return PromptResponse.model_validate(result)

        self._begin_turn()
        self.turn = asyncio.create_task(invoke())

    def _begin_turn(self) -> None:
        if not self._connector_meters():
            self.meter.begin()

    def start_turn(self, observation: JsonObject, new: list[JsonObject]) -> None:
        """Prompt the native session with only the notifications it has not been given yet;
        operator messages reach it as notifications too. The session's first prompt also carries
        the instructions and the full observation. Later prompts carry only the time, because the
        session already holds the tools, its earlier results and the world view."""
        assert self.connection is not None and self.client.session_id is not None
        ids = [str(notification["id"]) for notification in new]
        first = not self.session_prompted
        prompted = (
            {
                key: value
                for key, value in observation.items()
                if key not in {"messages", "notifications"}
            }
            if first
            else {"time": observation.get("time")}
        ) | {"notifications": json_value(new)}
        self.session_prompted = True
        self.delivered_notifications.update(ids)
        self.turn_delivered.extend(ids)
        self.client.reply.clear()
        self.client.metadata.clear()
        self.record(
            {
                "kind": "acp_prompt",
                "session_id": self.client.session_id,
                "time": observation.get("time"),
                "notifications": json_value(ids),
            }
        )
        self._begin_turn()
        self.turn = asyncio.create_task(
            self.connection.prompt(
                session_id=self.client.session_id,
                prompt=[
                    text_block(
                        (
                            INSTRUCTIONS
                            + "\n"
                            + (self.launch.instructions if self.launch else "")
                            + "\n"
                            if first
                            else ""
                        )
                        + (self.native.continuation or "")
                        + "\n"
                        + json.dumps(prompted)
                    )
                ],
            )
        )

    def _connector_meters(self) -> bool:
        return self.launch is not None and self.launch.meters_usage

    def _record_session_cost(self, session_id: str, cost_usd: float) -> None:
        if not self._connector_meters():
            self.meter.set_session_cost(session_id, cost_usd)

    def _meter_turn(self, turn: asyncio.Task, result: object) -> None:
        if self._connector_meters() or self.metered_turn is turn:
            return
        self.metered_turn = turn
        usage = getattr(result, "usage", None)
        if usage is None:
            self.meter.add_unmeasured()
        else:
            self.meter.add(usage.input_tokens, usage.output_tokens, None)

    async def finish_turn(self) -> JsonObject | None:
        """Close the ended native turn; return its report, or None when it ended silently."""
        assert self.turn is not None
        try:
            result = self.turn.result()
        except BaseException:
            self._meter_turn(self.turn, None)
            raise
        self._meter_turn(self.turn, result)
        native_turn = NativeTurn(
            stop_reason=result.stop_reason,
            response_metadata=json_object(getattr(result, "field_meta", None) or {}),
            update_metadata=self.client.metadata,
        )
        if self.launch is not None and self.launch.validate_turn is not None:
            self.launch.validate_turn(native_turn)
        if self.launch is not None and self.launch.after_turn is not None:
            self.native = await self.launch.after_turn(native_turn)
        if self.bridge is not None and self.bridge.has_pending and not self.native.status.busy:
            raise RuntimeError("native turn ended with unresolved world operations")
        self.turn = None
        self.record(
            {
                "kind": "acp_turn_end",
                "session_id": self.client.session_id,
                "stop_reason": result.stop_reason,
            }
        )
        if result.stop_reason != "end_turn":
            raise ValueError(f"ACP prompt did not complete: {result.stop_reason}")
        text = "".join(self.client.reply)
        return {"kind": "report", "message": text} if text else None

    def reply(self, action: JsonObject) -> ActionReply:
        delivered, self.turn_delivered = self.turn_delivered, []
        return ActionReply(
            kind="action",
            action=action_contract.validate_python(action),
            status=HarnessStatus(
                pending=self.turn is not None
                or self.native.status.pending
                or self.native.continuation is not None
                or self.native.request is not None,
                busy=self.native.status.busy
                and not (self.bridge is not None and self.bridge.pending is not None),
                next_wake=self.native.status.next_wake,
                delivered=delivered,
            ),
        )

    async def lifecycle(self, message: Lifecycle) -> LifecycleReply:
        if self.chat_id is not None and message.chat_id != self.chat_id:
            raise ValueError("lifecycle request belongs to another chat")
        capabilities = self.factory.capabilities
        if message.kind == "reset":
            if capabilities.reset_scope == "none" or message.scope != capabilities.reset_scope:
                raise ValueError("adapter does not support requested reset scope")
            await self.reset_chat()
        elif message.scope != "none":
            raise ValueError("non-reset lifecycle requires scope none")
        elif message.kind == "cancel":
            if not capabilities.cancel:
                raise ValueError("adapter does not declare cancel")
            await self.cancel_turn()
        else:
            await self.close()
        return LifecycleReply(kind=message.kind, scope=message.scope)

    async def reset_chat(self) -> None:
        await self.cancel_turn()
        if self.launch is not None and self.launch.reset is not None:
            await self.launch.reset()
        self.delivered_notifications.clear()
        self.turn_delivered.clear()
        self.client.reply.clear()
        self.client.metadata.clear()
        self.native = NativeObservation()
        if self.connection is not None:
            old_session = self.client.session_id
            self.client.session_id = None
            assert self.chat_id is not None
            self.bridge = ToolBridge(self.chat_id, self.record)
            await self.new_session()
            self.record(
                {
                    "kind": "acp_chat_reset",
                    "old_session_id": old_session,
                    "session_id": self.client.session_id,
                }
            )

    async def cancel_turn(self) -> None:
        try:
            await self.cancel_prompt()
        finally:
            self.native = NativeObservation()
            if self.launch is not None and self.launch.cancel is not None:
                await self.launch.cancel()

    async def cancel_prompt(self) -> None:
        turn = self.turn
        if turn is None:
            if self.bridge is not None:
                self.bridge.cancel_pending()
            return
        assert self.connection is not None and self.client.session_id is not None
        try:
            async with asyncio.timeout(5):
                await self.connection.cancel(session_id=self.client.session_id)
                if self.bridge is not None:
                    self.bridge.cancel_pending()
                await asyncio.shield(turn)
        finally:
            if self.bridge is not None:
                self.bridge.cancel_pending()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            if self.turn is turn:
                self.turn = None
            self._meter_turn(turn, None)
        self.record({"kind": "acp_cancelled", "session_id": self.client.session_id})

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.cancel_turn()
        finally:
            self.resources_stop.set()
            if self.resource_task is not None:
                if not self.resources_ready.is_set():
                    self.resource_task.cancel()
                await self.resource_task
            self.record({"kind": "acp_closed", "chat_id": self.chat_id})


async def run_config(config: AdapterConfig) -> None:
    namespace = os.environ.get("OVERNIGHT_CHAT_NAMESPACE")
    if namespace is not None:
        config = config.model_copy(update={"evidence": config.evidence / "chats" / namespace})
    connector = Connector(config, load_factory(config.factory))
    await serve(
        connector.factory.capabilities,
        connector.observe,
        connector.lifecycle,
        connector.close,
        connector.steer,
    )


@click.command()
@click.option("--config", "path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--config-json",
    "text",
    help="The adapter config itself, for a container that cannot mount the host's config file.",
)
def main(path: Path | None, text: str | None) -> None:
    """Run an external factory through the shared ACP host."""
    if (path is None) == (text is None):
        raise click.UsageError("pass exactly one of --config or --config-json")
    raw = path.read_bytes() if path is not None else text
    assert raw is not None
    config = AdapterConfig.model_validate_json(raw)
    asyncio.run(run_config(config))


if __name__ == "__main__":
    main()
