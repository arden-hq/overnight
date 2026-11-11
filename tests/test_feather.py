import asyncio
import json
import os
from collections import deque

import httpx
import pytest

pytest.importorskip("feather_harness")
pytest.importorskip("overnight_feather")
from feather_harness import ModelSettings, SystemMessage, UserMessage
from feather_harness.core.messages import AssistantMessage, ToolCall
from feather_harness.core.runner import RunResult, TraceSegment
from openai import AsyncOpenAI
from overnight_feather.host import relayed_client
from overnight_feather.provider import Budget, IdentifiedModel
from overnight_feather.session import Chat, Connector, Observe
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from overnight.adapters.bridge import ToolBridge
from overnight.adapters.http import serve_http
from overnight.adapters.provider import CompatibleProvider
from overnight.adapters.provider_relay import RequestBudget


def completion(calls=(), content=None):
    return {
        "id": "response",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": list(calls)},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.0001},
    }


def call(id, name, args):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def observation(now=0, receipts=()):
    return {
        "time": now,
        "horizon": 1000,
        "operations": {
            "records": [
                {
                    "name": "write",
                    "description": "Write",
                    "read_only": False,
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                }
            ]
        },
        "messages": [],
        "receipts": list(receipts),
        "world": {},
        "context": "test",
    }


def test_native_same_argument_calls_keep_distinct_ids_and_approval_continues(tmp_path):
    async def run():
        responses = iter(
            [
                completion(
                    [
                        call("first", "records__write", {"value": "same"}),
                        call("second", "records__write", {"value": "same"}),
                    ]
                ),
                completion(content="finished"),
            ]
        )

        async def provider(request):
            return httpx.Response(200, json=next(responses))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            async with AsyncOpenAI(
                api_key="test", base_url="https://model.invalid", http_client=http
            ) as client:
                connector = Connector(client, "openai/gpt-5.4-mini", tmp_path, 100)
                try:
                    first = await connector.observe(
                        Observe(kind="observe", chat_id="a", observation=observation())
                    )
                    first_id = first.action.id
                    pending = {
                        "id": first_id,
                        "invocation_id": first_id,
                        "status": "pending",
                        "time": 0,
                    }
                    waiting = await connector.observe(
                        Observe(
                            kind="observe", chat_id="a", observation=observation(receipts=[pending])
                        )
                    )
                    assert waiting.action.kind == "wait"
                    approved = {
                        "id": first_id,
                        "invocation_id": first_id,
                        "status": "approved",
                        "time": 0,
                    }
                    retry = await connector.observe(
                        Observe(
                            kind="observe",
                            chat_id="a",
                            observation=observation(receipts=[pending, approved]),
                        )
                    )
                    assert retry.action == first.action
                    result = {
                        "id": first_id,
                        "invocation_id": first_id,
                        "status": "succeeded",
                        "time": 0,
                        "value": {},
                        "error": None,
                    }
                    second = await connector.observe(
                        Observe(
                            kind="observe", chat_id="a", observation=observation(receipts=[result])
                        )
                    )
                    assert second.action.id != first_id
                    assert second.action.args == first.action.args
                    final = await connector.observe(
                        Observe(
                            kind="observe",
                            chat_id="a",
                            observation=observation(
                                receipts=[
                                    {
                                        "id": second.action.id,
                                        "invocation_id": second.action.id,
                                        "status": "succeeded",
                                        "time": 0,
                                        "value": {},
                                        "error": None,
                                    }
                                ]
                            ),
                        )
                    )
                    assert final.action.kind == "report"
                finally:
                    await connector.close()

    asyncio.run(run())


def test_wake_preserves_native_history_and_chat_reset_drops_it(tmp_path):
    async def run():
        requests = []
        responses = iter(
            [
                completion([call("wake", "schedule_wake", {"time": 600, "message": "follow up"})]),
                completion([call("question", "ask_operator", {"message": "Proceed?"})]),
                completion(content="continued"),
                completion(content="fresh"),
            ]
        )

        async def provider(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=next(responses))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            async with AsyncOpenAI(
                api_key="test", base_url="https://model.invalid", http_client=http
            ) as client:
                connector = Connector(client, "openai/gpt-5.4-mini", tmp_path, 100)
                try:
                    wake = await connector.observe(
                        Observe(kind="observe", chat_id="a", observation=observation())
                    )
                    assert wake.status.next_wake == 600
                    await connector.observe(
                        Observe(kind="observe", chat_id="a", observation=observation(600))
                    )
                    assert any(message["role"] == "tool" for message in requests[1]["messages"])
                    waiting = await connector.observe(
                        Observe(kind="observe", chat_id="a", observation=observation(601))
                    )
                    assert waiting.action.kind == "wait"
                    assert waiting.status.pending and not waiting.status.busy
                    answered = observation(602)
                    answered["messages"] = [{"time": 602, "message": "Proceed."}]
                    await connector.observe(
                        Observe(kind="observe", chat_id="a", observation=answered)
                    )
                    assert any(
                        message.get("tool_calls")
                        and message["tool_calls"][0]["function"]["name"] == "ask_operator"
                        for message in requests[2]["messages"]
                    )
                    await connector.remove(connector.chats["a"])
                    await connector.observe(
                        Observe(kind="observe", chat_id="a", observation=observation())
                    )
                    assert [message["role"] for message in requests[3]["messages"]] == [
                        "system",
                        "user",
                    ]
                finally:
                    await connector.close()

    asyncio.run(run())


def test_budget_refuses_unaffordable_call_before_provider():
    budget = Budget(limit=0.001)
    with pytest.raises(RuntimeError):
        budget.check([UserMessage(content="x" * 1000)], (), ModelSettings(max_tokens=4096))


def test_model_client_reaches_the_provider_only_through_the_relay(monkeypatch):
    seen = []

    async def completions(request: Request):
        seen.append(request.headers.get("authorization"))
        return JSONResponse(completion(content="done"))

    upstream = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    monkeypatch.setenv("EXAMPLE_API_KEY", "private-test-key")
    records = []
    budget = RequestBudget(input_usd_per_million=0.2, output_usd_per_million=0.75)

    async def run():
        async with serve_http(upstream) as url:
            provider = CompatibleProvider(base_url=url + "/v1", api_key_env="EXAMPLE_API_KEY")
            async with relayed_client(provider, "example/model", budget, records.append) as client:
                assert client.api_key != "private-test-key"
                assert not str(client.base_url).startswith(url)
                assert "EXAMPLE_API_KEY" not in os.environ
                model = IdentifiedModel(
                    calls=deque(),
                    budget=Budget(limit=1),
                    record=lambda _value: None,
                    client=client,
                    model="example/model",
                )
                return await model.complete(
                    [UserMessage(content="hi")], settings=ModelSettings(max_tokens=100)
                )

    result = asyncio.run(run())

    assert result.assistant.content == "done"
    assert seen == ["Bearer private-test-key"]
    assert records == [
        {"kind": "provider_request", "request": 1, "status": 200},
        {"kind": "provider_closed", "requests": 1},
    ]


def test_native_tools_do_not_consume_world_operation_identity():
    async def run():
        responses = iter(
            [
                completion(
                    [call("native", "todo", {}), call("world", "records__write", {"value": "x"})]
                ),
                completion(content="finished"),
            ]
        )

        async def provider(request):
            return httpx.Response(200, json=next(responses))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            async with AsyncOpenAI(
                api_key="test", base_url="https://model.invalid", http_client=http
            ) as client:
                calls = deque()
                model = IdentifiedModel(
                    calls=calls,
                    tracked_tools=frozenset({"records__write"}),
                    budget=Budget(limit=1),
                    record=lambda value: None,
                    client=client,
                    model="test",
                    replay_reasoning=False,
                )
                await model.complete(
                    (UserMessage(content="go"),), tools=(), settings=ModelSettings(max_tokens=100)
                )
                assert calls.popleft().id == "world"
                await model.complete(
                    (UserMessage(content="next"),), tools=(), settings=ModelSettings(max_tokens=100)
                )
                assert not calls

    asyncio.run(run())


def test_children_with_same_native_call_id_keep_distinct_dispatch_and_receipt():
    async def run():
        records = []
        bridge = ToolBridge("chat", records.append)
        bridge.run_id = "root-run"
        left = Chat(id="chat", bridge=bridge, native_agent_id="left", record=records.append)
        right = Chat(id="chat", bridge=bridge, native_agent_id="right", record=records.append)
        left.calls.append(ToolCall(id="same", name="records__write", arguments="{}"))
        right.calls.append(ToolCall(id="same", name="records__write", arguments="{}"))
        left_identity = left.identity("records__write")
        right_identity = right.identity("records__write")
        assert left_identity.tool_call_id != right_identity.tool_call_id
        assert left_identity.parent_call_id == right_identity.parent_call_id == "same"
        assert left_identity.identity_source == right_identity.identity_source == "adapter"
        left_result = asyncio.create_task(bridge.invoke("records", "write", {}, left_identity))
        right_result = asyncio.create_task(bridge.invoke("records", "write", {}, right_identity))
        first = await bridge.next_action()
        bridge.resolve(
            [{"id": first["id"], "invocation_id": first["id"], "status": "denied", "time": 0}]
        )
        assert (await left_result)["status"] == "denied"
        second = await bridge.next_action()
        bridge.resolve(
            [
                {
                    "id": second["id"],
                    "invocation_id": second["id"],
                    "status": "succeeded",
                    "time": 0,
                    "value": {"ok": True},
                    "error": None,
                }
            ]
        )
        assert (await right_result)["value"] == {"ok": True}
        assert records[0]["native_agent_id"] == "left"
        assert records[1]["native_agent_id"] == "right"

    asyncio.run(run())


def test_retained_context_keeps_compacted_segment_without_restoring_discarded_history(tmp_path):
    async def run():
        summary = SystemMessage(content="Compacted continuation")
        final = AssistantMessage(content="finished")
        result = RunResult(
            stop_reason="completed",
            final_answer="finished",
            num_turns=2,
            compactions=1,
            segments=(
                TraceSegment(
                    kind="execution",
                    input=(UserMessage(content="discarded long history"),),
                    messages=(),
                ),
                TraceSegment(
                    kind="compaction",
                    input=(UserMessage(content="summarize"),),
                    messages=(AssistantMessage(content="summary"),),
                ),
                TraceSegment(kind="execution", input=(summary,), messages=(final,)),
            ),
        )
        async with AsyncOpenAI(api_key="unused") as client:
            connector = Connector(client, "openai/gpt-5.4-mini", tmp_path, 100)
            chat = Chat(id="chat", bridge=ToolBridge("chat", lambda value: None))
            chat.run = asyncio.create_task(asyncio.sleep(0, result=result))
            await chat.run
            connector.finish_run(chat)
            assert chat.history == (summary, final)

    asyncio.run(run())


def test_cancelled_pending_operation_keeps_native_chat_usable(tmp_path):
    async def run():
        responses = iter(
            [
                completion([call("blocked", "records__write", {"value": "cancelled"})]),
                completion([call("resumed", "records__write", {"value": "resumed"})]),
                completion(content="finished"),
            ]
        )

        async def provider(request):
            return httpx.Response(200, json=next(responses))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            async with AsyncOpenAI(
                api_key="test", base_url="https://model.invalid", http_client=http
            ) as client:
                connector = Connector(client, "openai/gpt-5.4-mini", tmp_path, 100)
                try:
                    first = await connector.observe(
                        Observe(kind="observe", chat_id="chat", observation=observation())
                    )
                    waiting = observation(
                        receipts=[
                            {
                                "id": first.action.id,
                                "invocation_id": first.action.id,
                                "status": "pending",
                                "time": 0,
                            }
                        ]
                    )
                    await connector.observe(
                        Observe(kind="observe", chat_id="chat", observation=waiting)
                    )
                    await connector.cancel(connector.chats["chat"])
                    resumed = observation(3)
                    resumed["messages"] = [
                        {"message": "The earlier task was cancelled. Write resumed."}
                    ]
                    next_action = await connector.observe(
                        Observe(kind="observe", chat_id="chat", observation=resumed)
                    )
                    assert next_action.action.args == {"value": "resumed"}
                    receipt = {
                        "id": next_action.action.id,
                        "invocation_id": next_action.action.id,
                        "status": "succeeded",
                        "value": {},
                        "error": None,
                        "time": 3,
                    }
                    resumed["receipts"] = [receipt]
                    final = await connector.observe(
                        Observe(kind="observe", chat_id="chat", observation=resumed)
                    )
                    assert final.action.message == "finished"
                finally:
                    await connector.close()

    asyncio.run(run())
