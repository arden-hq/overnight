import json

import anyio
import httpx
import pytest
from httpx_sse import EventSource
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from overnight.adapters.http import serve_http
from overnight.adapters.provider import CompatibleProvider
from overnight.adapters.provider_relay import (
    CapturedCall,
    ProviderRelay,
    RequestBudget,
    read_capture,
)

MODEL = "engine/model"
MESSAGE = {
    "role": "assistant",
    "content": "Checking the order.",
    "reasoning_content": None,
    "tool_calls": [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "orders_get", "arguments": '{"id": "A-7"}'},
        },
        {
            "id": "call-2",
            "type": "function",
            "function": {"name": "mail_search", "arguments": '{"query": "vendor"}'},
        },
    ],
}
USAGE = {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}
# The upstream SSE an engine sends when it streams; capture off must pass it on unchanged.
ENGINE_STREAM = (
    b'data: {"id": "gen-1", "object": "chat.completion.chunk", "choices": '
    b'[{"index": 0, "delta": {"content": "hi"}, "finish_reason": null}]}\n\n'
    b'data: {"id": "gen-1", "object": "chat.completion.chunk", "choices": [], '
    b'"usage": {"prompt_tokens": 4, "completion_tokens": 1}}\n\n'
    b"data: [DONE]\n\n"
)


def engine(received: list[dict], token_ids: bool = True) -> Starlette:
    """A fake vLLM-style OpenAI-compatible server."""

    async def completions(request: Request) -> Response:
        body = json.loads(await request.body())
        received.append(body)
        if body.get("stream"):
            return Response(ENGINE_STREAM, media_type="text/event-stream")
        said = body["messages"][-1]["content"]
        if said == "overloaded":
            return JSONResponse({"error": {"message": "engine overloaded"}}, status_code=503)
        choice = {
            "index": 0,
            "message": MESSAGE,
            "finish_reason": "tool_calls",
            "logprobs": {
                "content": [
                    {"token": "a", "logprob": -0.5, "top_logprobs": []},
                    {"token": "b", "logprob": -0.25, "top_logprobs": []},
                    {"token": "c", "logprob": 0.0, "top_logprobs": []},
                ]
            },
        }
        if said == "no logprobs":
            choice["logprobs"] = None
        completion = {
            "id": f"gen-{len(received)}",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [choice | ({"token_ids": [7, 8, 9]} if token_ids else {})],
            "usage": USAGE,
        }
        if token_ids:
            completion["prompt_token_ids"] = [1, 2, 3, 4]
        return JSONResponse(completion)

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])


async def relay_calls(capture, requests: list[tuple[str, dict]], token_ids: bool = True):
    """Serve a fake engine and a relay on loopback; send each (path, body) as the harness."""
    received: list[dict] = []
    records: list[dict] = []
    answers: list[httpx.Response] = []
    async with serve_http(engine(received, token_ids)) as upstream_url:
        budget = RequestBudget(input_usd_per_million=0.2, output_usd_per_million=0.75)
        provider = CompatibleProvider(base_url=f"{upstream_url}/v1", api_key_env="ENGINE_KEY")
        async with httpx.AsyncClient() as client:
            relay = ProviderRelay(
                budget, provider, MODEL, "engine-key", client, records.append, capture=capture
            )
            async with serve_http(relay.app) as relay_url:
                headers = {"Authorization": f"Bearer {relay.token}"}
                for path, body in requests:
                    answer = await client.post(f"{relay_url}{path}", json=body, headers=headers)
                    answers.append(answer)
    return received, records, answers


def replayed(answer: httpx.Response) -> tuple[list[dict], dict]:
    """Parse a chat-completions SSE stream the way an OpenAI client accumulates it."""
    events = list(EventSource(answer).iter_sse())
    assert events[-1].data == "[DONE]"
    chunks = [json.loads(event.data) for event in events[:-1]]
    message: dict = {"content": "", "tool_calls": {}}
    finish = None
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["id"] and chunk["model"] == MODEL
        for choice in chunk["choices"]:
            delta = choice["delta"]
            message["content"] += delta.get("content") or ""
            message.setdefault("role", delta.get("role"))
            for call in delta.get("tool_calls", []):
                slot = message["tool_calls"].setdefault(
                    call["index"], {"function": {"name": "", "arguments": ""}}
                )
                slot["id"] = call.get("id", slot.get("id"))
                slot["type"] = call.get("type", slot.get("type"))
                slot["function"]["name"] += call["function"].get("name", "")
                slot["function"]["arguments"] += call["function"].get("arguments", "")
            finish = choice["finish_reason"] or finish
    message["tool_calls"] = [
        message["tool_calls"][index] for index in sorted(message["tool_calls"])
    ]
    return chunks, message | {"finish_reason": finish}


def test_capture_stores_engine_tokens_and_replays_the_harness_stream(tmp_path):
    capture = tmp_path / "captured.jsonl"
    messages = [{"role": "user", "content": "Where is order A-7?"}]
    streaming = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": [{"type": "function", "function": {"name": "orders_get"}}],
    }
    received, records, (streamed, plain) = anyio.run(
        relay_calls,
        capture,
        [
            ("/v1/chat/completions", streaming),
            ("/v1/chat/completions", {"model": MODEL, "messages": messages}),
        ],
    )

    for body in received:
        assert body["stream"] is False and "stream_options" not in body
        assert body["logprobs"] is True and body["return_token_ids"] is True
        assert body["max_completion_tokens"] == 2048
    assert read_capture(capture).calls == [
        CapturedCall(
            request=number,
            generation_id=f"gen-{number}",
            prompt_token_ids=[1, 2, 3, 4],
            completion_token_ids=[7, 8, 9],
            logprobs="returned",
            completion_logprobs=[-0.5, -0.25, 0.0],
            finish_reason="tool_calls",
        )
        for number in (1, 2)
    ]
    assert [record["kind"] for record in records].count("provider_captured") == 2

    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    chunks, message = replayed(streamed)
    assert message == {
        "role": "assistant",
        "content": MESSAGE["content"],
        "tool_calls": MESSAGE["tool_calls"],
        "finish_reason": "tool_calls",
    }
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"] == USAGE
    assert "token_ids" not in streamed.text

    completion = plain.json()
    assert completion["choices"][0]["message"] == MESSAGE
    assert "prompt_token_ids" not in completion and "token_ids" not in completion["choices"][0]


def test_capture_fails_closed_without_token_ids_and_for_responses(tmp_path):
    capture = tmp_path / "captured.jsonl"
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    received, records, (missing, responses) = anyio.run(
        relay_calls,
        capture,
        [("/v1/chat/completions", body), ("/v1/responses", {"model": MODEL, "input": "hi"})],
        False,
    )

    assert [missing.status_code, responses.status_code] == [502, 400]
    assert len(received) == 1
    stored = read_capture(capture)
    assert stored.calls == []
    assert [(call.request, call.reason) for call in stored.unanswered] == [
        (1, "capture_missing"),
        (None, "capture_api"),
    ]
    assert {record.get("reason") for record in records} >= {"capture_missing", "capture_api"}


def test_capture_passes_engine_errors_on_and_records_missing_logprobs(tmp_path):
    capture = tmp_path / "captured.jsonl"

    def body(content: str) -> dict:
        return {"model": MODEL, "messages": [{"role": "user", "content": content}], "stream": True}

    received, records, (failed, answered) = anyio.run(
        relay_calls,
        capture,
        [
            ("/v1/chat/completions", body("overloaded")),
            ("/v1/chat/completions", body("no logprobs")),
        ],
    )

    assert failed.status_code == 503
    assert failed.json() == {"error": {"message": "engine overloaded"}}
    assert {"kind": "provider_request", "request": 1, "status": 503} in records
    assert answered.status_code == 200
    (call,) = read_capture(capture).calls
    assert call.request == 2
    assert call.logprobs == "not_returned" and call.completion_logprobs is None
    assert call.completion_token_ids == [7, 8, 9]


def test_without_capture_the_relay_forwards_the_stream_unchanged(tmp_path):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    received, records, (answer,) = anyio.run(relay_calls, None, [("/v1/chat/completions", body)])

    assert received == [body | {"max_completion_tokens": 2048}]
    assert answer.content == ENGINE_STREAM
    assert [record["kind"] for record in records] == [
        "provider_request",
        "provider_generation",
        "provider_usage",
    ]
    assert list(tmp_path.iterdir()) == []


def test_a_call_the_harness_gave_up_on_ends_at_the_engine_and_is_stored_unanswered(tmp_path):
    capture = tmp_path / "calls.jsonl"

    async def run(capture) -> tuple[list[str], list[dict]]:
        ended: list[str] = []
        records: list[dict] = []

        async def completions(request: Request) -> Response:
            # The relay's call ends when its own client, the harness, has left.
            await request.body()
            with anyio.move_on_after(10):
                while (await request.receive())["type"] != "http.disconnect":
                    pass
                ended.append("left")
            return JSONResponse({})

        slow = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
        async with serve_http(slow) as upstream_url:
            budget = RequestBudget(input_usd_per_million=0.2, output_usd_per_million=0.75)
            provider = CompatibleProvider(base_url=f"{upstream_url}/v1", api_key_env="ENGINE_KEY")
            async with httpx.AsyncClient(timeout=60) as client:
                relay = ProviderRelay(
                    budget, provider, MODEL, "engine-key", client, records.append, capture=capture
                )
                async with serve_http(relay.app) as relay_url:
                    with pytest.raises(httpx.ReadTimeout):
                        await client.post(
                            f"{relay_url}/v1/chat/completions",
                            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": f"Bearer {relay.token}"},
                            timeout=0.5,
                        )
                    with anyio.fail_after(5):
                        while not ended:
                            await anyio.sleep(0.05)
        return ended, records

    for path in (None, capture):
        ended, records = anyio.run(run, path)
        assert ended == ["left"]
        assert records == [{"kind": "provider_abandoned", "request": 1}]
    assert [call.reason for call in read_capture(capture).unanswered] == ["engine_unreachable"]
