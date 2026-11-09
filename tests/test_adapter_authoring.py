import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from overnight.adapters.bridge import OperationCancelled, ToolBridge
from overnight.execution.tool_server import TrustedToolContext


def test_receipt_bridge_rejects_forged_context_and_reexecution():
    async def exercise():
        bridge = ToolBridge("chat", lambda record: None)
        bridge.run_id = "native-session"
        wrong = TrustedToolContext(run_id="native-session", tool_call_id="one", chat_id="other")
        with pytest.raises(ValueError, match="context"):
            await bridge.invoke("mail", "send", {}, wrong)
        native = TrustedToolContext(run_id="native-session", tool_call_id="one", chat_id="chat")
        call = asyncio.create_task(bridge.invoke("mail", "send", {}, native))
        action = await bridge.next_action()
        bridge.resolve(
            [{"id": action["id"], "invocation_id": action["id"], "status": "denied", "time": 0}]
        )
        assert (await call)["status"] == "denied"
        with pytest.raises(ValueError, match="already dispatched"):
            await bridge.invoke("mail", "send", {}, native)
        assert not bridge.has_pending

    asyncio.run(exercise())


def test_acp_client_refuses_host_access_and_permission_escalation():
    pytest.importorskip("acp")
    from acp import RequestError
    from acp.schema import PermissionOption, ToolCallProgress

    from overnight.adapters.acp_session import NativeClient

    async def exercise():
        client = NativeClient(lambda record: None)
        with pytest.raises(RequestError):
            await client.read_text_file(session_id="native", path="/private/evaluator.json")
        refused = await client.request_permission(
            "native",
            ToolCallProgress(tool_call_id="call", session_update="tool_call_update"),
            [PermissionOption(option_id="allow", name="Allow", kind="allow_once")],
        )
        assert refused.outcome.outcome == "cancelled"

    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["no-usage", "cancelled"])
def test_a_turn_without_usage_makes_earlier_totals_unknown(tmp_path, ending):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.adapters.evidence import UsageMeter
    from overnight.execution.harness import Capabilities
    from overnight.execution.usage import UNMEASURED, ProcessUsage

    async def unused_factory(context):
        raise AssertionError("no process is needed for this usage contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities()),
        )
        target = tmp_path / "usage.json"
        connector.meter = UsageMeter(str(target))
        connector.meter.add(3, 2, None)
        connector.turn = asyncio.get_running_loop().create_future()
        connector.turn.set_result(SimpleNamespace(stop_reason="end_turn"))
        if ending == "cancelled":

            async def cancel(session_id):
                return None

            connector.connection = SimpleNamespace(cancel=cancel)
            connector.client.session_id = "native"
            await connector.cancel_prompt()
        else:
            await connector.finish_turn()
        assert ProcessUsage.model_validate_json(target.read_text()) == UNMEASURED

    asyncio.run(exercise())


def test_a_turn_that_raised_is_counted_once(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.adapters.evidence import UsageMeter
    from overnight.execution.harness import Capabilities

    async def unused_factory(context):
        raise AssertionError("no process is needed for this usage contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities()),
        )
        connector.meter = UsageMeter(str(tmp_path / "usage.json"))
        connector.turn = asyncio.get_running_loop().create_future()
        usage = SimpleNamespace(input_tokens=3, output_tokens=2)
        connector.turn.set_result(SimpleNamespace(stop_reason="end_turn", usage=usage))

        def refuse(turn):
            raise ValueError("native turn is not valid")

        connector.launch = SimpleNamespace(meters_usage=False, validate_turn=refuse)
        for _ in range(2):
            with pytest.raises(ValueError):
                await connector.finish_turn()
        assert connector.meter.usage.calls == 1

    asyncio.run(exercise())


def test_continuation_uses_same_native_session_without_replaying_old_notifications(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.execution.harness import Capabilities

    class Session:
        def __init__(self):
            self.prompts = []

        async def prompt(self, session_id, prompt):
            self.prompts.append((session_id, prompt[0].text))
            return SimpleNamespace(stop_reason="end_turn")

    async def unused_factory(context):
        raise AssertionError("no native process is needed for this continuation contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=Path(tmp_path / "native")),
            Factory(
                prepare=unused_factory,
                capabilities=Capabilities(chat_isolation=True, cancel=True, reset_scope="chat"),
            ),
        )
        session = Session()
        connector.connection = session
        connector.client.session_id = "native"
        connector.bridge = ToolBridge("chat", connector.record)
        first = _operator_notification("step-0", "Remember unique-codeword", 0)
        second = _operator_notification("step-1", "Use the earlier codeword", 1)
        for observation in (
            {"time": 0, "notifications": [first], "receipts": []},
            {"time": 1, "notifications": [first, second], "receipts": []},
        ):
            assert (await connector.advance(observation)).action.kind == "done"
        assert session.prompts[0][0] == session.prompts[1][0] == "native"
        assert "unique-codeword" not in session.prompts[1][1]
        assert "Use the earlier codeword" in session.prompts[1][1]
        await connector.close()

    asyncio.run(exercise())


def test_an_idle_native_session_is_prompted_only_for_a_new_notification(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.execution.harness import Capabilities

    prompts = []

    async def prompt(session_id, prompt):
        prompts.append(prompt[0].text)
        return SimpleNamespace(stop_reason="end_turn")

    async def unused_factory(context):
        raise AssertionError("no native process is needed for this prompt contract")

    def observe(time, notifications):
        mail = {"as_of": time, "messages": []}
        world = {"time": time, "actor_id": "agent", "results": {}, "services": {"mail": mail}}
        return {"time": time, "world": world, "notifications": notifications, "receipts": []}

    request = _operator_notification("step-0", "Ask the vendor", 0)

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=Path(tmp_path / "native")),
            Factory(prepare=unused_factory, capabilities=Capabilities()),
        )
        connector.connection = SimpleNamespace(prompt=prompt)
        connector.client.session_id = "native"
        connector.bridge = ToolBridge("main", connector.record)
        first = await connector.advance(observe(0, [request]))
        assert (first.action.kind, first.status.delivered) == ("done", [request["id"]])
        later = await connector.advance(observe(3, []))
        assert (later.action.kind, later.status.delivered) == ("done", [])
        assert len(prompts) == 1 and "Ask the vendor" in prompts[0]
        assert "World services" in prompts[0] and '"world"' in prompts[0]
        reply = _operator_notification("step-1", "Vendor replied", 5)
        woken = await connector.advance(observe(5, [request, reply]))
        assert woken.status.delivered == [reply["id"]]
        assert len(prompts) == 2 and "Vendor replied" in prompts[1]
        assert "Ask the vendor" not in prompts[1]
        assert "World services" not in prompts[1] and '"world"' not in prompts[1]
        await connector.close()

    asyncio.run(exercise())


def _operator_notification(message_id, text, time):
    return {
        "id": f"n-{message_id}",
        "time": time,
        "kind": "operator_message",
        "service_id": None,
        "ref": message_id,
        "summary": text,
        "chat_id": None,
    }


def test_cancellation_releases_the_pending_tool_and_keeps_native_session(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.execution.harness import Capabilities

    async def unused_factory(context):
        raise AssertionError("no native startup in this lifecycle unit contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=Path(tmp_path / "native")),
            Factory(
                prepare=unused_factory,
                capabilities=Capabilities(chat_isolation=True, cancel=True, reset_scope="chat"),
            ),
        )
        finished = asyncio.get_running_loop().create_future()

        async def prompt():
            return await finished

        async def native_cancel(session_id):
            assert session_id == "retained"
            finished.set_result(SimpleNamespace(stop_reason="cancelled"))

        connector.connection = SimpleNamespace(cancel=native_cancel)
        connector.client.session_id = "retained"
        connector.turn = asyncio.create_task(prompt())
        connector.bridge = ToolBridge("chat", lambda record: None)
        connector.bridge.run_id = "retained"
        invocation = asyncio.create_task(
            connector.bridge.invoke(
                "mail",
                "send",
                {},
                TrustedToolContext(run_id="retained", tool_call_id="blocked", chat_id="chat"),
            )
        )
        await connector.bridge.next_action()
        queued = asyncio.create_task(
            connector.bridge.invoke(
                "mail",
                "send",
                {},
                TrustedToolContext.for_operation("retained", "chat", parent_call_id="python-call"),
            )
        )
        await asyncio.sleep(0)
        await connector.cancel_turn()
        results = await asyncio.gather(invocation, queued, return_exceptions=True)
        assert all(isinstance(result, OperationCancelled) for result in results)
        assert not connector.bridge.has_pending
        assert connector.client.session_id == "retained"
        assert connector.turn is None and not connector.closed
        await connector.close()

    asyncio.run(exercise())


def test_nested_operations_preserve_identity_receipts_and_chat_isolation():
    async def exercise():
        records = []
        bridge = ToolBridge("chat", records.append)
        bridge.run_id = "session"
        contexts = [
            TrustedToolContext.for_operation("session", "chat", parent_call_id="python-call")
            for _ in range(2)
        ]
        arguments = {"body": "same"}
        calls = [
            asyncio.create_task(bridge.invoke("mail", "send", arguments, identity))
            for identity in contexts
        ]
        first = await bridge.next_action()
        arguments["body"] = "changed after submission"
        assert first["args"] == {"body": "same"}
        assert first["id"] != f"session:{contexts[1].tool_call_id}"
        assert bridge.has_pending
        with pytest.raises(ValueError):
            bridge.resolve(
                [{"id": first["id"], "invocation_id": "foreign", "status": "denied", "time": 0}]
            )
        assert not any(call.done() for call in calls)
        pending = {"id": first["id"], "invocation_id": first["id"], "status": "pending", "time": 0}
        assert bridge.resolve([pending]) == {"kind": "wait"}
        waiting = pending | {"status": "waiting", "until": 5}
        assert bridge.resolve([waiting]) == {"kind": "wait"}
        approved = pending | {"status": "approved"}
        assert bridge.resolve([pending, approved]) == first
        assert not any(call.done() for call in calls)

        other = ToolBridge("other-chat", records.append)
        other.run_id = "other-session"
        other_context = TrustedToolContext.for_operation("other-session", "other-chat")
        other_call = asyncio.create_task(other.invoke("mail", "send", {}, other_context))
        other_action = await other.next_action()
        denied = {
            "id": other_action["id"],
            "invocation_id": other_action["id"],
            "status": "denied",
            "time": 0,
        }
        other.resolve([denied])
        assert await other_call == denied

        result = {
            "id": first["id"],
            "invocation_id": first["id"],
            "status": "succeeded",
            "time": 1,
            "value": {"sent": True},
            "error": None,
        }
        bridge.resolve([result])
        assert await calls[0] == result
        second = await bridge.next_action()
        assert second["id"] != first["id"] and second["args"] == first["args"]
        refused = denied | {"id": second["id"], "invocation_id": second["id"]}
        bridge.resolve([refused])
        assert await calls[1] == refused
        assert not bridge.has_pending
        dispatches = [record for record in records if record["kind"] == "adapter_dispatch"]
        assert len(dispatches) == 3
        assert [record.get("parent_call_id") for record in dispatches] == [
            "python-call",
            None,
            "python-call",
        ]
        assert all(record["identity_source"] == "adapter" for record in dispatches)
        assert not any(record["kind"] == "native_dispatch" for record in records)

    asyncio.run(exercise())


def test_cancelled_queued_operation_never_dispatches_or_replays():
    async def exercise():
        bridge = ToolBridge("chat", lambda record: None)
        bridge.run_id = "session"
        cancelled = TrustedToolContext.for_operation("session", "chat")
        first = asyncio.create_task(bridge.invoke("mail", "send", {}, cancelled))
        await asyncio.sleep(0)
        first.cancel()
        surviving = TrustedToolContext.for_operation("session", "chat")
        second = asyncio.create_task(bridge.invoke("mail", "send", {}, surviving))
        action = await bridge.next_action()
        assert action["id"] == f"session:{surviving.tool_call_id}"
        bridge.cancel_pending()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert isinstance(results[1], OperationCancelled)
        assert not bridge.has_pending
        with pytest.raises(ValueError, match="replayable"):
            await bridge.invoke("mail", "send", {}, cancelled)

    asyncio.run(exercise())


def test_native_extension_drains_world_receipts_before_completion(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, NativeObservation, NativeRequest
    from overnight.execution.harness import Capabilities, HarnessStatus

    async def unused_factory(context):
        raise AssertionError("no native process needed for this wire contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:adapter", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities(simulated_clock=True)),
        )
        connector.chat_id = "chat"
        connector.client.session_id = "session"
        connector.bridge = ToolBridge("chat", connector.record)
        connector.bridge.run_id = "session"

        async def native_extension(method, params):
            receipt = await connector.bridge.invoke(
                "mail", "send", {}, TrustedToolContext.for_operation("session", "chat")
            )
            assert receipt["status"] == "denied"
            return {"stopReason": "end_turn"}

        connector.connection = SimpleNamespace(ext_method=native_extension)
        connector.native = NativeObservation(
            status=HarnessStatus(pending=True),
            request=NativeRequest(method="test/advance", params={"time": 7}),
        )
        first = await connector.advance({"notifications": [], "receipts": []})
        assert first.action.kind == "invoke"
        refused = {
            "id": first.action.id,
            "invocation_id": first.action.id,
            "status": "denied",
            "time": 7,
        }
        second = await connector.advance({"notifications": [], "receipts": [refused]})
        assert second.action.kind == "done"
        assert not connector.bridge.has_pending
        await connector.close()

    asyncio.run(exercise())


def test_background_native_call_dispatches_after_parent_turn_without_another_prompt(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, Launch, NativeObservation
    from overnight.execution.harness import Capabilities, HarnessStatus
    from overnight.execution.provenance import CodeIdentity

    async def unused_factory(context):
        raise AssertionError("no process is needed for this background receipt contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="external:adapter", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities()),
        )
        active = True
        changed = asyncio.Event()
        prompts = []

        async def native_state(_value):
            return NativeObservation(status=HarnessStatus(pending=active, busy=active))

        async def prompt(**kwargs):
            prompts.append(kwargs)
            return SimpleNamespace(stop_reason="end_turn")

        connector.connection = SimpleNamespace(prompt=prompt)
        connector.client.session_id = "native"
        connector.bridge = ToolBridge("chat", connector.record)
        connector.bridge.run_id = "native"
        connector.launch = Launch(
            command=["unused"],
            cwd=tmp_path,
            adapter_code=CodeIdentity(git_commit=None, sha256="a" * 64, version=None),
            observe=native_state,
            after_turn=native_state,
            wait_for_activity=changed.wait,
        )
        observation = {"time": 0, "notifications": [], "receipts": []}
        connector.start_turn(observation, [])
        await connector.turn
        await connector.finish_turn()

        async def child():
            nonlocal active
            receipt = await connector.bridge.invoke(
                "records", "add", {}, TrustedToolContext.for_operation("native", "chat")
            )
            active = False
            changed.set()
            return receipt

        work = asyncio.create_task(child())
        proposed = await connector.advance(observation)
        assert proposed.action.kind == "invoke"
        assert proposed.status.pending and not proposed.status.busy
        receipt = {
            "id": proposed.action.id,
            "invocation_id": proposed.action.id,
            "status": "succeeded",
            "time": 3,
            "value": {"stored": True},
            "error": None,
        }
        settled = await connector.advance({"notifications": [], "receipts": [receipt]})
        assert await work == receipt
        assert settled.action.kind == "done" and not settled.status.pending
        assert len(prompts) == 1
        await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("work", ["continuation", "request"])
def test_native_input_keeps_the_runner_open_after_the_parent_turn(tmp_path, work):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, NativeObservation, NativeRequest
    from overnight.execution.harness import Capabilities

    async def unused_factory(context):
        raise AssertionError("no native startup is needed to project an obligation")

    connector = Connector(
        AdapterConfig(factory="external:adapter", evidence=tmp_path / "native"),
        Factory(prepare=unused_factory, capabilities=Capabilities()),
    )
    connector.native = (
        NativeObservation(continuation="Owned native child completed; its report is available.")
        if work == "continuation"
        else NativeObservation(request=NativeRequest(method="native/advance", params={}))
    )
    assert connector.reply({"kind": "done"}).status.pending


def test_chat_reset_cancels_old_calls_and_rebinds_native_session(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, Launch
    from overnight.adapters.protocol import Lifecycle
    from overnight.execution.harness import Capabilities
    from overnight.execution.provenance import CodeIdentity

    async def unused_factory(context):
        raise AssertionError("no native process needed for reset boundary")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:adapter", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities(reset_scope="chat")),
        )
        connector.chat_id = "chat"
        connector.client.session_id = "old"
        connector.delivered_notifications = {"n-secret"}
        connector.bridge = ToolBridge("chat", connector.record)
        connector.bridge.run_id = "old"
        old_bridge = connector.bridge
        old = asyncio.create_task(
            old_bridge.invoke("mail", "send", {}, TrustedToolContext.for_operation("old", "chat"))
        )
        await old_bridge.next_action()

        async def new_session(**kwargs):
            return SimpleNamespace(session_id="new")

        connector.connection = SimpleNamespace(new_session=new_session)
        connector.launch = Launch(
            command=["unused"],
            cwd=tmp_path,
            adapter_code=CodeIdentity(git_commit=None, sha256="a" * 64, version=None),
        )
        await connector.lifecycle(Lifecycle(kind="reset", chat_id="chat", scope="chat"))
        assert isinstance(
            (await asyncio.gather(old, return_exceptions=True))[0], OperationCancelled
        )
        assert connector.client.session_id == connector.bridge.run_id == "new"
        assert connector.delivered_notifications == set()
        with pytest.raises(ValueError):
            await connector.bridge.invoke(
                "mail", "send", {}, TrustedToolContext.for_operation("old", "chat")
            )
        await connector.close()

    asyncio.run(exercise())


def test_external_client_can_handle_native_permissions_without_changing_default_denial(tmp_path):
    pytest.importorskip("acp")
    from acp import RequestError
    from acp.schema import (
        AllowedOutcome,
        PermissionOption,
        RequestPermissionResponse,
        ToolCallProgress,
    )

    from overnight.adapters.acp import Connector
    from overnight.adapters.acp_session import NativeClient
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.execution.harness import Capabilities

    class ExplicitNativeClient(NativeClient):
        async def request_permission(self, session_id, tool_call, options, **kwargs):
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id="once")
            )

    async def unused_factory(context):
        raise AssertionError("no native process needed for client policy boundary")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="external:adapter", evidence=tmp_path / "native"),
            Factory(
                prepare=unused_factory,
                capabilities=Capabilities(),
                client_type=ExplicitNativeClient,
            ),
        )
        response = await connector.client.request_permission(
            "native",
            ToolCallProgress(tool_call_id="call", session_update="tool_call_update"),
            [PermissionOption(option_id="once", name="Allow once", kind="allow_once")],
        )
        assert response.outcome.option_id == "once"
        with pytest.raises(RequestError):
            await connector.client.read_text_file(
                session_id="native", path="/private/evaluator.json"
            )
        await connector.close()

    asyncio.run(exercise())


def test_native_handshake_preserves_external_capabilities_and_session_configuration(
    tmp_path, monkeypatch
):
    pytest.importorskip("acp")
    from contextlib import asynccontextmanager

    from acp import PROTOCOL_VERSION
    from acp.connection import StreamDirection, StreamEvent
    from acp.schema import ClientCapabilities, FileSystemCapabilities, ReadTextFileResponse

    from overnight.adapters import acp as frontend
    from overnight.adapters.acp_session import NativeClient
    from overnight.adapters.config import AdapterConfig, Factory, Launch
    from overnight.adapters.protocol import Observe
    from overnight.execution.harness import Capabilities
    from overnight.execution.provenance import CodeIdentity

    class WorkspaceClient(NativeClient):
        def capabilities(self):
            return ClientCapabilities(fs=FileSystemCapabilities(read_text_file=True))

        def stream_observers(self):
            return [self.capture_native_event]

        def capture_native_event(self, event):
            self.record({"kind": "native_transport_observed", "method": event.message["method"]})

        async def read_text_file(self, session_id, path, **kwargs):
            return ReadTextFileResponse(content="native workspace content")

    class NativeConnection:
        async def initialize(self, protocol_version, client_capabilities):
            assert client_capabilities.fs.read_text_file is True
            self.negotiated = True
            return SimpleNamespace(protocol_version=PROTOCOL_VERSION, agent_info=None)

        async def new_session(self, **kwargs):
            assert kwargs["integration"] == {"turn_limit": 8, "isolated_tools": True}
            return SimpleNamespace(session_id="native")

    connection = NativeConnection()

    @asynccontextmanager
    async def spawn(*args, **kwargs):
        assert len(kwargs["observers"]) == 1
        kwargs["observers"][0](
            StreamEvent(StreamDirection.INCOMING, {"method": "integration/child"})
        )
        yield connection, SimpleNamespace(pid=1)

    class ToolServer:
        def __init__(self, operations, invoke, **kwargs):
            self.token = "scoped"

        @asynccontextmanager
        async def serve(self):
            yield "http://local"

    async def prepare(context):
        return Launch(
            command=["unused"],
            cwd=tmp_path,
            adapter_code=CodeIdentity(git_commit=None, sha256="a" * 64, version=None),
            session_metadata={"integration": {"turn_limit": 8, "isolated_tools": True}},
        )

    async def exercise():
        connector = frontend.Connector(
            AdapterConfig(factory="external:adapter", evidence=tmp_path / "native"),
            Factory(prepare=prepare, capabilities=Capabilities(), client_type=WorkspaceClient),
        )
        await connector.start(
            Observe(kind="observe", chat_id="chat", observation={"operations": {}})
        )
        assert connection.negotiated
        response = await connector.client.read_text_file(session_id="native", path="marker.txt")
        assert response.content == "native workspace content"
        await connector.close()

    monkeypatch.setattr(frontend, "WorldToolServer", ToolServer)
    monkeypatch.setattr(frontend, "spawn_agent_process", spawn)
    asyncio.run(exercise())


def test_connector_closes_real_mcp_context_from_its_resource_owner(tmp_path, monkeypatch):
    pytest.importorskip("acp")
    from contextlib import asynccontextmanager

    from acp import PROTOCOL_VERSION

    from overnight.adapters import acp as frontend
    from overnight.adapters.config import AdapterConfig, Factory, Launch
    from overnight.adapters.protocol import Observe
    from overnight.execution.harness import Capabilities
    from overnight.execution.provenance import CodeIdentity

    prompt_started = asyncio.Event()
    prompt_released = asyncio.Event()

    class NativeConnection:
        async def initialize(self, **kwargs):
            return SimpleNamespace(protocol_version=PROTOCOL_VERSION, agent_info=None)

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="native")

        async def prompt(self, **kwargs):
            prompt_started.set()
            await prompt_released.wait()
            return SimpleNamespace(stop_reason="cancelled")

        async def cancel(self, **kwargs):
            prompt_released.set()

    @asynccontextmanager
    async def spawn(*args, **kwargs):
        yield NativeConnection(), SimpleNamespace(pid=1)

    async def prepare(context):
        return Launch(
            command=["unused"],
            cwd=tmp_path,
            adapter_code=CodeIdentity(git_commit=None, sha256="a" * 64, version=None),
        )

    async def exercise():
        connector = frontend.Connector(
            AdapterConfig(factory="external:adapter", evidence=tmp_path / "native-owner"),
            Factory(prepare=prepare, capabilities=Capabilities()),
        )
        observation = Observe(
            kind="observe",
            chat_id="chat",
            observation={
                "time": 0,
                "operations": {},
                "notifications": [_operator_notification("step-0", "go", 0)],
                "receipts": [],
            },
        )
        running = asyncio.create_task(connector.observe(observation))
        await prompt_started.wait()
        await connector.close()
        result = (await asyncio.gather(running, return_exceptions=True))[0]
        assert isinstance(result, BaseException)
        assert "different task" not in str(result)

    monkeypatch.setattr(frontend, "spawn_agent_process", spawn)
    asyncio.run(exercise())


def test_active_turn_observes_world_time_before_receipt_can_resume_native_work(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, NativeObservation
    from overnight.execution.harness import Capabilities, HarnessStatus

    async def unused_factory(context):
        raise AssertionError("no process is needed for this observation contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:adapter", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities()),
        )
        clock = 0

        async def observe(observation):
            nonlocal clock
            clock = observation["time"]
            return NativeObservation(status=HarnessStatus(pending=True, busy=True))

        def resolve(receipts):
            assert clock == 125
            return {"kind": "wait", "until": 180}

        active = asyncio.get_running_loop().create_future()
        connector.turn = active
        connector.launch = SimpleNamespace(observe=observe)
        connector.bridge = SimpleNamespace(pending=object(), resolve=resolve)
        reply = await connector.advance({"time": 125, "receipts": [], "notifications": []})
        assert reply.status.pending
        assert not reply.status.busy
        connector.bridge.pending = None
        assert connector.reply({"kind": "wait"}).status.busy
        assert connector.turn is active
        active.cancel()

    asyncio.run(exercise())


def _unused_launch(tmp_path, **hooks):
    from overnight.adapters.config import Launch
    from overnight.execution.provenance import CodeIdentity

    return Launch(
        command=["unused"],
        cwd=tmp_path,
        adapter_code=CodeIdentity(git_commit=None, sha256="a" * 64, version=None),
        **hooks,
    )


def test_cancel_during_correlated_observe_is_acknowledged_by_the_real_connector(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.adapters.host import _Router
    from overnight.adapters.protocol import Hello, Lifecycle, Observe
    from overnight.domain.contracts import HarnessRequirements
    from overnight.execution.harness import ActionReply, Capabilities, LifecycleReply

    async def unused_factory(context):
        raise AssertionError("no native startup in this routing contract")

    async def exercise():
        capabilities = Capabilities(cancel=True, steering=True)
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=capabilities),
        )
        finished = asyncio.get_running_loop().create_future()

        async def prompt(session_id, prompt):
            return await finished

        async def native_cancel(session_id):
            finished.set_result(SimpleNamespace(stop_reason="cancelled"))

        async def native_cleanup():
            await asyncio.sleep(0.01)

        connector.connection = SimpleNamespace(prompt=prompt, cancel=native_cancel)
        connector.launch = _unused_launch(tmp_path, cancel=native_cleanup)
        connector.client.session_id = "native"
        connector.chat_id = "chat"
        connector.bridge = ToolBridge("chat", connector.record)
        connector.bridge.run_id = "native"
        written = []

        async def write(reply, message_id):
            written.append(reply)

        router = _Router(
            capabilities, connector.observe, connector.lifecycle, connector.steer, write
        )
        async with asyncio.TaskGroup() as tasks:
            await router.route(
                Hello(
                    kind="hello",
                    version=1,
                    required=HarnessRequirements(
                        simulated_clock=False, chat_isolation=False, steering=True
                    ),
                ),
                tasks,
            )
            observation = {
                "time": 0,
                "notifications": [_operator_notification("step-0", "work", 0)],
                "receipts": [],
            }
            await router.route(
                Observe(
                    kind="observe",
                    chat_id="chat",
                    observation=observation,
                    session_id="host-session",
                    turn_id="host-turn",
                    message_id="host-turn",
                ),
                tasks,
            )
            while connector.turn is None:
                await asyncio.sleep(0)
            assert await router.route(Lifecycle(kind="cancel", chat_id="chat"), tasks)
        assert written[-1] == LifecycleReply(kind="cancel")
        assert not any(isinstance(reply, ActionReply) for reply in written)
        assert connector.turn is None and router.active is None
        await connector.close()

    asyncio.run(exercise())


def test_background_call_submitted_during_activity_refresh_is_not_lost(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, NativeObservation
    from overnight.execution.harness import Capabilities, HarnessStatus

    async def unused_factory(context):
        raise AssertionError("no process is needed for this background admission contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities()),
        )
        activity = asyncio.Event()
        activity.set()
        refreshes = 0
        submitted = []

        async def native_state(_value):
            nonlocal refreshes
            refreshes += 1
            if refreshes == 2:
                context = TrustedToolContext.for_operation("native", "chat")
                submitted.append(
                    asyncio.create_task(connector.bridge.invoke("records", "add", {}, context))
                )
                for _ in range(5):
                    await asyncio.sleep(0)
            return NativeObservation(status=HarnessStatus(pending=True, busy=True))

        connector.connection = SimpleNamespace()
        connector.client.session_id = "native"
        connector.bridge = ToolBridge("chat", connector.record)
        connector.bridge.run_id = "native"
        connector.launch = _unused_launch(
            tmp_path, observe=native_state, wait_for_activity=activity.wait
        )
        proposed = await connector.advance({"time": 0, "notifications": [], "receipts": []})
        assert proposed.action.kind == "invoke"
        assert connector.bridge.pending is not None
        assert connector.bridge.pending.action.id == proposed.action.id
        await connector.close()
        assert isinstance(
            (await asyncio.gather(*submitted, return_exceptions=True))[0], OperationCancelled
        )

    asyncio.run(exercise())


def test_steering_without_an_active_native_turn_never_reaches_the_native_hook(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.adapters.protocol import Steer
    from overnight.execution.harness import Capabilities

    async def unused_factory(context):
        raise AssertionError("no native startup in this steering contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=tmp_path / "native"),
            Factory(prepare=unused_factory, capabilities=Capabilities(steering=True)),
        )
        steered = []

        async def native_steer(message):
            steered.append(message)

        connector.launch = _unused_launch(tmp_path, steer=native_steer)
        with pytest.raises(ValueError, match="no active native turn"):
            await connector.steer(
                Steer(
                    kind="steer",
                    request_id="request",
                    chat_id="chat",
                    session_id="host-session",
                    turn_id="host-turn",
                    message="correction",
                    message_id="request",
                )
            )
        assert steered == []
        await connector.close()

    asyncio.run(exercise())


def test_declared_capabilities_require_their_native_implementation(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory
    from overnight.adapters.protocol import Observe
    from overnight.execution.harness import Capabilities

    async def prepare(context):
        return _unused_launch(tmp_path)

    config = AdapterConfig(factory="unused:prepare", evidence=tmp_path / "native")
    with pytest.raises(ValueError, match="reset scope"):
        Connector(
            config, Factory(prepare=prepare, capabilities=Capabilities(reset_scope="external"))
        )

    async def exercise():
        connector = Connector(
            config, Factory(prepare=prepare, capabilities=Capabilities(steering=True))
        )
        try:
            with pytest.raises(ValueError, match="steer hook"):
                await connector.start(
                    Observe(kind="observe", chat_id="chat", observation={"operations": {}})
                )
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_a_native_wake_after_the_horizon_waits_for_the_end_instead(tmp_path):
    pytest.importorskip("acp")
    from overnight.adapters.acp import Connector
    from overnight.adapters.config import AdapterConfig, Factory, NativeObservation
    from overnight.execution.harness import Capabilities, HarnessStatus

    async def unused_factory(context):
        raise AssertionError("no native process is needed for this wake contract")

    async def exercise():
        connector = Connector(
            AdapterConfig(factory="unused:prepare", evidence=Path(tmp_path / "native")),
            Factory(prepare=unused_factory, capabilities=Capabilities(simulated_clock=True)),
        )
        connector.bridge = ToolBridge("main", connector.record)
        connector.native = NativeObservation(status=HarnessStatus(pending=True, next_wake=60))
        assert await connector.idle_action({"time": 0, "horizon": 20}) == {"kind": "wait"}
        assert await connector.idle_action({"time": 0, "horizon": 90}) == {
            "kind": "wait",
            "until": 60,
        }
        await connector.close()

    asyncio.run(exercise())
