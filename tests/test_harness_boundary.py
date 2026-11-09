import json
import signal
import subprocess
import sys

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from overnight.domain.contracts import HarnessRequirements
from overnight.execution.adapters import AdapterError
from overnight.execution.harness import HarnessAdapter
from overnight.execution.tool_server import NATIVE_CONTEXT_KEY, ToolOperation, WorldToolServer


def test_mcp_catalog_identity_and_private_transport():
    async def exercise():
        calls = []

        async def invoke(service, operation, arguments, context):
            calls.append((service, operation, arguments, context))
            return {"status": "succeeded"}

        server = WorldToolServer(
            {"mail": [ToolOperation(name="read", input_schema={"type": "object"})]},
            invoke,
            identity_required=True,
        )
        async with server.serve() as base:
            async with httpx.AsyncClient() as unauthenticated:
                assert (await unauthenticated.post(f"{base}/mail", json={})).status_code == 401
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {server.token}"}
            ) as client:
                async with streamable_http_client(f"{base}/mail", http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert [tool.name for tool in (await session.list_tools()).tools] == [
                            "read"
                        ]
                        rejected = await session.call_tool("read", {})
                        assert rejected.isError and not calls
                        forged = await session.call_tool(
                            "read", {NATIVE_CONTEXT_KEY: {"run_id": "model-authored"}}
                        )
                        malformed = await session.call_tool(
                            "read",
                            {},
                            meta={NATIVE_CONTEXT_KEY: {"run_id": "", "tool_call_id": "t"}},
                        )
                        assert forged.isError and malformed.isError and not calls
                        meta = {
                            NATIVE_CONTEXT_KEY: {
                                "run_id": "r",
                                "tool_call_id": "t",
                                "chat_id": "c",
                            }
                        }
                        accepted = await session.call_tool("read", {}, meta=meta)
                        assert not accepted.isError
                        assert calls[0][:3] == ("mail", "read", {})
                        assert calls[0][3].tool_call_id == "t"

    anyio.run(exercise)


def _child(
    tmp_path,
    capabilities,
    observe="{'kind':'action','action':{'kind':'wait'},'status':{'pending':False}}",
):
    child = tmp_path / "harness.py"
    child.write_text(
        "import json,os,sys\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        f" if request['kind']=='hello': reply={{'kind':'ready','version':1,'capabilities':{capabilities!r}}}\n"
        f" elif request['kind']=='observe': reply={observe}\n"
        " else: reply={'kind':request['kind'],'scope':request['scope']}\n"
        " print(json.dumps(reply),flush=True)\n"
        " if request['kind']=='close': break\n"
    )
    return [sys.executable, str(child)]


def test_missing_clock_fails_before_observation(tmp_path):
    command = _child(tmp_path, {"chat_isolation": True, "cancel": True})
    adapter = HarnessAdapter(command, 2, None, HarnessRequirements())
    try:
        with pytest.raises(AdapterError, match="simulated_clock"):
            adapter.respond({"time": 0})
    finally:
        adapter.close()


def test_process_reset_does_not_claim_native_memory_reset(tmp_path):
    command = _child(
        tmp_path,
        {
            "simulated_clock": True,
            "chat_isolation": True,
            "cancel": True,
            "reset_scope": "process",
        },
    )
    adapter = HarnessAdapter(command, 2, None, HarnessRequirements())
    try:
        with pytest.raises(AdapterError, match="native chat"):
            adapter.reset()
    finally:
        adapter.close()


def test_native_reset_cancel_and_close_are_acknowledged(tmp_path):
    command = _child(
        tmp_path,
        {
            "simulated_clock": True,
            "chat_isolation": True,
            "cancel": True,
            "reset_scope": "chat",
        },
    )
    with HarnessAdapter(command, 2, None, HarnessRequirements(reset_scope="chat")) as adapter:
        assert adapter.respond({"time": 0}) == {"kind": "wait"}
        adapter.reset()
        adapter.cancel()
        process = adapter.process
    assert process.returncode == 0


def test_native_pending_work_blocks_its_lane_and_completion(tmp_path):
    from test_runtime import BASE

    from overnight.domain.contracts import DoneAction, WaitAction
    from overnight.execution.harness import HarnessStatus
    from overnight.execution.runtime import Block, Trial
    from overnight.execution.scenario import resolve_scenario

    command = _child(tmp_path, {"simulated_clock": True, "chat_isolation": True, "cancel": True})
    with HarnessAdapter(command, 2, None, HarnessRequirements()) as adapter:
        trial = Trial(resolve_scenario(BASE), adapter)
        (lane,) = trial.lanes
        adapter.status = HarnessStatus(pending=True, busy=True)
        trial.act(WaitAction(kind="wait"))
        assert lane.block is None
        with pytest.raises(AdapterError, match="pending"):
            trial.act(DoneAction(kind="done"))
        adapter.status = HarnessStatus(pending=True, busy=False, next_wake=4)
        trial.act(WaitAction(kind="wait"))
        assert lane.block == Block(until=4)
        adapter.status = HarnessStatus(pending=True, busy=False)
        trial.act(WaitAction(kind="wait", until=6))
        assert lane.block == Block(until=6)
        with pytest.raises(AdapterError, match="pending"):
            trial.act(DoneAction(kind="done"))
        adapter.status = HarnessStatus(pending=False, next_wake=20)
        trial.act(DoneAction(kind="done"))
        assert (lane.status, trial.world.now, trial.status) == ("idle", 0, "running")


def _done_child(tmp_path):
    done = "{'kind':'action','action':{'kind':'done'},'status':{'pending':False}}"
    capabilities = {"simulated_clock": True, "chat_isolation": True, "cancel": True}
    return _child(tmp_path, capabilities, done)


def _observed_times(result):
    return [entry["time"] for entry in result.trace if entry["kind"] == "agent_observation"]


def test_an_idle_harness_is_woken_only_by_a_notification_for_it(tmp_path):
    import copy

    from test_runtime import BASE, VENDOR, vendor_mail

    from overnight.execution.runtime import run_scenario
    from overnight.execution.scenario import resolve_scenario

    scenario = copy.deepcopy(BASE)
    scenario["actors"].append(VENDOR)
    scenario["events"] = [vendor_mail(2, to="operator"), vendor_mail(4)]
    with HarnessAdapter(_done_child(tmp_path), 2, None, HarnessRequirements()) as adapter:
        result = run_scenario(resolve_scenario(scenario), adapter)
    assert (result.status, _observed_times(result)) == ("done", [0, 4])


def test_idle_periods_do_not_spend_the_step_budget(tmp_path):
    import copy

    from test_runtime import BASE, VENDOR, vendor_mail

    from overnight.execution.runtime import run_scenario
    from overnight.execution.scenario import resolve_scenario

    scenario = copy.deepcopy(BASE)
    scenario["max_steps"] = 3
    scenario["actors"].append(VENDOR)
    scenario["events"] = [vendor_mail(at, to="operator") for at in range(1, 6)] + [vendor_mail(6)]
    with HarnessAdapter(_done_child(tmp_path), 2, None, HarnessRequirements()) as adapter:
        result = run_scenario(resolve_scenario(scenario), adapter)
    assert (result.status, result.steps, _observed_times(result)) == ("done", 2, [0, 6])


def test_a_waiting_harness_is_woken_early_only_by_a_notification_it_has_not_been_shown():
    import copy

    from test_runtime import BASE, VENDOR, ScriptedHarness, vendor_mail

    from overnight.execution.runtime import run_scenario
    from overnight.execution.scenario import resolve_scenario

    scenario = copy.deepcopy(BASE)
    scenario["actors"].append(VENDOR)
    scenario["events"] = [vendor_mail(4)]
    scenario["operator"]["steps"] = [{"at": 0, "kind": "message", "message": "Not prompted"}]
    harness = ScriptedHarness([({"kind": "wait", "until": 8}, {"pending": True})])
    result = run_scenario(resolve_scenario(scenario), harness)
    assert result.status == "done"
    assert [observation["time"] for observation in harness.observations] == [0, 4]


def test_an_agents_own_effect_does_not_wake_it():
    import copy

    from test_runtime import BASE, ScriptedHarness

    from overnight.execution.runtime import run_scenario
    from overnight.execution.scenario import resolve_scenario

    scenario = copy.deepcopy(BASE)
    scenario["actors"].append({"id": "vendor", "role": "person"})
    args = {"to": ["vendor"], "cc": ["agent"], "subject": "Refund?", "body": "Can we cancel?"}
    send = {"kind": "invoke", "id": "ask", "service_id": "mail", "operation": "send_message"}
    scenario["operator"] = {
        "policy": {
            "default": "deny",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [
            {
                "at": 2,
                "kind": "approve",
                "match": {"service_id": "mail", "operation": "send_message", "args": args},
            }
        ],
    }
    harness = ScriptedHarness(
        [
            (send | {"args": args}, {"pending": True}),
            ({"kind": "wait"}, {"pending": True}),
            (send | {"args": args}, {"pending": True}),
            ({"kind": "done"}, {"pending": False}),
        ]
    )
    result = run_scenario(resolve_scenario(scenario), harness)
    assert (result.status, result.steps) == ("done", 4)
    assert [observation["time"] for observation in harness.observations] == [0, 0, 2, 2]
    assert not [entry for entry in result.trace if entry["kind"] == "notification"]


def test_an_event_after_the_horizon_does_not_keep_an_idle_lane_open(tmp_path):
    import copy

    from test_runtime import BASE, VENDOR, vendor_mail

    from overnight.execution.runtime import run_scenario
    from overnight.execution.scenario import resolve_scenario

    scenario = copy.deepcopy(BASE)
    scenario["actors"].append(VENDOR)
    late = vendor_mail(9) | {"dependencies": ["mail-at-8-call"], "delay": 5}
    del late["at"]
    scenario["events"] = [vendor_mail(8, to="operator"), late]
    with HarnessAdapter(_done_child(tmp_path), 2, None, HarnessRequirements()) as adapter:
        result = run_scenario(resolve_scenario(scenario), adapter)
    assert (result.status, _observed_times(result)) == ("done", [0])
    assert result.final_state["pending_events"] == ["mail-at-9"]
    assert result.trace[-1]["time"] == 8


def test_an_idle_episode_task_waits_only_for_world_events_before_its_deadline(tmp_path):
    import copy

    from test_episode import BASE, task
    from test_runtime import VENDOR, vendor_mail

    from overnight.execution.runtime import run_scenario
    from overnight.execution.scenario import resolve_scenario

    doc = copy.deepcopy(BASE)
    doc["actors"].append(VENDOR)
    doc["events"] = [vendor_mail(4)]
    doc["tasks"] = [
        task("before", "chat-a", "Ends before the reply", deadline=3),
        task("after", "chat-b", "Sees the reply", deadline=10),
    ]
    adapters = {}
    for chat in ("chat-a", "chat-b"):
        folder = tmp_path / chat
        folder.mkdir()
        adapters[chat] = HarnessAdapter(_done_child(folder), 2, None, HarnessRequirements())
    try:
        result = run_scenario(resolve_scenario(doc), adapters=adapters)
    finally:
        for adapter in adapters.values():
            adapter.close()
    ended = {
        item["task_id"]: (item["status"], item["completed_at"]) for item in result.task_outcomes
    }
    assert ended == {"before": ("done", 3), "after": ("done", 4)}


def test_capability_failure_precedes_operator_message(tmp_path):
    import copy

    from test_runtime import BASE

    from overnight.execution.runtime import Trial
    from overnight.execution.scenario import resolve_scenario

    scenario = copy.deepcopy(BASE)
    scenario["operator"]["steps"] = [{"kind": "message", "at": 0, "message": "private"}]
    command = _child(tmp_path, {"simulated_clock": False})
    with HarnessAdapter(command, 2, None, HarnessRequirements()) as adapter:
        trial = Trial(resolve_scenario(scenario), adapter)
        trial.execute()
        assert trial.status == "invalid"
        assert trial.messages == []


def test_episode_binds_distinct_chat_identity_and_process_namespaces(tmp_path):
    from test_episode import BASE, task

    from overnight.execution.episode import Episode
    from overnight.execution.scenario import resolve_scenario

    command = _child(
        tmp_path,
        {"simulated_clock": True, "chat_isolation": True, "cancel": True},
        "{'kind':'action','action':{'kind':'report','message':json.dumps("
        "[request['chat_id'],os.environ['OVERNIGHT_CHAT_ID'],"
        "os.environ['OVERNIGHT_CHAT_NAMESPACE']])},'status':{'pending':False}}",
    )
    scenario = resolve_scenario(
        {**BASE, "tasks": [task("first", "chat-a", "One"), task("second", "chat-b", "Two")]}
    )
    with (
        HarnessAdapter(command, 2, None, HarnessRequirements()) as first,
        HarnessAdapter(command, 2, None, HarnessRequirements()) as second,
    ):
        Episode(scenario, {"chat-a": first, "chat-b": second})
        first_message = first.respond({})["message"]
        second_message = second.respond({})["message"]
        assert isinstance(first_message, str) and isinstance(second_message, str)
        first_context = json.loads(first_message)
        second_context = json.loads(second_message)
        assert first_context[:2] == ["chat-a", "chat-a"]
        assert second_context[:2] == ["chat-b", "chat-b"]
        assert first_context[2] != second_context[2]
        with pytest.raises(AdapterError, match="different episode chats"):
            Episode(scenario, {"chat-a": first, "chat-b": first})


def test_sidecar_ignores_a_late_result_for_an_abandoned_call(capsys):
    from overnight.execution.tool_sidecar import Call, Invocations, Result

    async def exercise():
        invocations = Invocations()
        call = Call(service="mail", operation="send", arguments={}, context=None)
        async with anyio.create_task_group() as group:
            group.start_soon(invocations.invoke, call)
            await anyio.wait_all_tasks_blocked()
            group.cancel_scope.cancel()
        invocations.deliver(Result(kind="result", id=call.id, result={"status": "succeeded"}))
        with pytest.raises(ValueError, match="pending"):
            invocations.deliver(Result(kind="result", id="never-issued", result={}))

    anyio.run(exercise)
    assert json.loads(capsys.readouterr().out)["id"]


def test_owned_http_server_leaves_process_termination_to_the_owner(tmp_path):
    owner = tmp_path / "owner.py"
    owner.write_text(
        "import anyio, httpx\n"
        "from starlette.applications import Starlette\n"
        "from starlette.routing import Route\n"
        "from overnight.adapters.http import serve_http\n"
        "entered = anyio.Event()\n"
        "async def blocked(request):\n"
        "    entered.set()\n"
        "    await anyio.sleep_forever()\n"
        "async def main():\n"
        "    async with serve_http(Starlette(routes=[Route('/', blocked)])) as endpoint:\n"
        "        async with httpx.AsyncClient() as client, anyio.create_task_group() as group:\n"
        "            group.start_soon(client.get, endpoint)\n"
        "            await entered.wait()\n"
        "            print('ready', flush=True)\n"
        "            await anyio.sleep_forever()\n"
        "anyio.run(main)\n"
    )
    process = subprocess.Popen([sys.executable, str(owner)], stdout=subprocess.PIPE)
    try:
        assert process.stdout is not None and process.stdout.readline() == b"ready\n"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == -signal.SIGTERM
    finally:
        process.kill()
        process.wait()
