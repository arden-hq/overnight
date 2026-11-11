"""A loopback relay that holds the provider key and bounds a native harness's paid requests.

The native process gets the relay's URL and a per-run token instead of the key. The relay forwards
Chat Completions (`/v1/chat/completions`) and Responses (`/v1/responses`) requests, streaming or
not, to the configured provider. It refuses, before anything is sent upstream, a request over the
byte limit, any request after `max_requests` were sent, request fields outside a standard set,
hosted tools, image, file or audio parts, references to provider-stored context and an unexpected model. It replaces the
output bound with `max_tokens`. `RequestBudget` checks that these limits fit `max_cost_usd` at the
caller's rates, so the cost bound holds before money is spent; it is not provider billing.

Each decision is recorded through `record`; the key is never recorded.

With a `capture` path (off by default, never set for evaluations) the relay serves Chat Completions
only. It calls the engine without streaming, asks for token IDs (`return_token_ids`, a vLLM
extension) and logprobs, appends one `CapturedCall` per call to that JSON Lines file, and answers
the harness in the shape it asked for: a JSON completion, or the same completion replayed as a
chat-completions event stream. A call the engine did not answer with a usable completion is
appended as an `UnansweredCall` instead, so the file is the whole record of the policy's calls.
Because nothing arrives until the whole generation is done, a captured call waits up to
`capture_timeout_seconds` for the engine instead of the client's own timeout. `read_capture`
reads a run's file back. A connector passes `run_capture()`, the
path the runner sets only for a run that asks for capture.
"""

import asyncio
import json
import os
import secrets
from collections.abc import Callable, Coroutine, Iterator
from functools import cache
from pathlib import Path
from typing import Any, Literal

import httpx
from httpx_sse import EventSource
from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from overnight.adapters.evidence import UsageMeter
from overnight.adapters.provider import Provider, endpoint
from overnight.domain.contracts import Contract, JsonObject
from overnight.domain.transport import CAPTURE_PATH_VARIABLE

type Api = Literal["chat", "responses"]

request_contract = TypeAdapter(JsonObject)

# Standard fields only. OpenRouter routing and spend controls such as `models`, `provider`,
# `plugins`, `route`, `transforms` and `n`, a paid `service_tier`, and Responses state such as
# `previous_response_id` or `conversation` could bypass the pinned model or the budget.
ALLOWED_REQUEST_FIELDS: dict[Api, frozenset[str]] = {
    "chat": frozenset(
        {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream",
            "stream_options",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "seed",
            "frequency_penalty",
            "presence_penalty",
            "response_format",
            "reasoning",
            "reasoning_effort",
            "usage",
            "store",
            "prompt_cache_key",
            "user",
        }
    ),
    "responses": frozenset(
        {
            "model",
            "input",
            "instructions",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream",
            "stream_options",
            "temperature",
            "top_p",
            "max_output_tokens",
            "reasoning",
            "text",
            "store",
            "include",
            "metadata",
            "truncation",
            "prompt_cache_key",
            "safety_identifier",
            "user",
        }
    ),
}
# Hosted tools (web search, code interpreter, remote MCP and similar) bill outside the token bound
# and act outside the world.
LOCAL_TOOL_TYPES = frozenset({"function", "custom"})
# A short URL or file ID can stand for many billed tokens, so the byte bound covers text only.
MEDIA_TYPES = frozenset({"image_url", "input_image", "input_file", "input_audio", "file"})


# Fields a token-returning engine adds that the harness never asked for.
CAPTURE_RESPONSE_FIELDS = frozenset({"prompt_token_ids", "prompt_logprobs", "kv_transfer_params"})


type LogprobsOutcome = Literal["returned", "not_returned", "incomplete"]


class CapturedCall(Contract):
    """One captured model call: the engine's own prompt and completion tokens."""

    request: int = Field(ge=1)
    """The relay's request number in this run, from 1."""
    generation_id: str | None
    prompt_token_ids: list[int]
    completion_token_ids: list[int]
    logprobs: LogprobsOutcome
    """`returned`: `completion_logprobs` holds one value per completion token. `not_returned`:
    the engine sent no logprobs. `incomplete`: it sent some, but not one number per token. Only
    `returned` carries values; the relay never fills in missing ones."""
    completion_logprobs: list[float] | None
    """The sampled token's logprob at each completion position, when `logprobs` is `returned`."""
    finish_reason: str | None

    @model_validator(mode="after")
    def logprobs_align(self):
        if (self.logprobs == "returned") != (self.completion_logprobs is not None):
            raise ValueError("completion logprobs are present exactly when they were returned")
        if self.completion_logprobs is not None and len(self.completion_logprobs) != len(
            self.completion_token_ids
        ):
            raise ValueError("completion logprobs must align with completion token ids")
        return self


type Unanswered = Literal[
    "engine_error", "engine_unreachable", "no_completion", "capture_missing", "capture_api"
]


class UnansweredCall(Contract):
    """A policy call the engine did not answer with a usable completion, under capture.

    `engine_error`: the engine answered with an error status. `engine_unreachable`: the call
    failed or was abandoned before the engine answered. `no_completion`: a success without a
    completion. `capture_missing`: a completion without token IDs. `capture_api`: a Responses
    request, which capture cannot serve; it never reached the engine and has no number."""

    request: int | None = Field(ge=1)
    reason: Unanswered
    status: int | None = None


class Capture(Contract):
    """A capture file read back: the answered calls and the unanswered ones, each in order."""

    calls: list[CapturedCall]
    unanswered: list[UnansweredCall]


captured_line = TypeAdapter(CapturedCall | UnansweredCall)


@cache
def run_capture() -> Path | None:
    """This process's capture file, which the runner names only for a run that captures.

    The first call takes the variable out of the process environment, so a harness or tool
    process the connector starts later does not inherit it; later calls return the same path."""
    target = os.environ.pop(CAPTURE_PATH_VARIABLE, None)
    return Path(target) if target else None


def read_capture(path: Path) -> Capture:
    """What a capturing relay stored at `path`. A missing file holds no calls; a line that is
    neither a `CapturedCall` nor an `UnansweredCall` raises `ValueError`."""
    lines = path.read_text().splitlines() if path.exists() else []
    entries = [captured_line.validate_json(line) for line in lines if line]
    return Capture(
        calls=[entry for entry in entries if isinstance(entry, CapturedCall)],
        unanswered=[entry for entry in entries if isinstance(entry, UnansweredCall)],
    )


def _append(capture: Path, entry: CapturedCall | UnansweredCall) -> None:
    descriptor = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as output:
        output.write(entry.model_dump_json() + "\n")


def _captured_call(request: int, completion: JsonObject) -> CapturedCall | None:
    """The tokens of a non-streamed completion with token IDs, or None when they are missing."""
    choices = completion.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    logprobs = choice.get("logprobs")
    content = logprobs.get("content") if isinstance(logprobs, dict) else None
    values = (
        [entry.get("logprob") if isinstance(entry, dict) else None for entry in content]
        if isinstance(content, list)
        else []
    )
    identifier = completion.get("id")
    reason = choice.get("finish_reason")
    tokens = choice.get("token_ids")
    floats = [
        float(value)
        for value in values
        if isinstance(value, int | float) and not isinstance(value, bool)
    ]
    complete = (
        isinstance(tokens, list) and len(floats) == len(values) == len(tokens) and bool(values)
    )
    try:
        return CapturedCall.model_validate(
            {
                "request": request,
                "generation_id": identifier if isinstance(identifier, str) else None,
                "prompt_token_ids": completion.get("prompt_token_ids"),
                "completion_token_ids": tokens,
                "logprobs": "returned"
                if complete
                else ("incomplete" if values else "not_returned"),
                "completion_logprobs": floats if complete else None,
                "finish_reason": reason if isinstance(reason, str) else None,
            }
        )
    except ValidationError:
        return None


def _harness_completion(completion: JsonObject) -> JsonObject:
    """The completion without the token data the relay asked for on the harness's behalf."""
    choices = completion.get("choices")
    return {
        key: value for key, value in completion.items() if key not in CAPTURE_RESPONSE_FIELDS
    } | {
        "choices": [
            {key: value for key, value in choice.items() if key != "token_ids"} | {"logprobs": None}
            if isinstance(choice, dict)
            else choice
            for choice in (choices if isinstance(choices, list) else [])
        ]
    }


def _replayed_stream(completion: JsonObject, include_usage: bool) -> Iterator[str]:
    """A chat-completions event stream that carries the same message as `completion`."""
    head: JsonObject = {
        key: completion[key]
        for key in ("id", "created", "model", "system_fingerprint")
        if completion.get(key) is not None
    } | {"object": "chat.completion.chunk"}

    def event(choices: list[JsonValue], **extra: JsonValue) -> str:
        return f"data: {json.dumps(head | {'choices': choices} | extra)}\n\n"

    choices = completion.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        index = choice.get("index", 0)
        message = choice.get("message")
        fields = message if isinstance(message, dict) else {}
        delta: JsonObject = {
            key: value for key, value in fields.items() if value is not None and key != "tool_calls"
        }
        calls = fields.get("tool_calls")
        if isinstance(calls, list) and calls:
            delta["tool_calls"] = [
                {"index": position} | call if isinstance(call, dict) else call
                for position, call in enumerate(calls)
            ]
        yield event([{"index": index, "delta": delta, "finish_reason": None}])
        yield event([{"index": index, "delta": {}, "finish_reason": choice.get("finish_reason")}])
    usage = completion.get("usage")
    if include_usage and usage is not None:
        yield event([], usage=usage)
    yield "data: [DONE]\n\n"


class RequestBudget(Contract):
    """Per-run request limits and the rates that bound their cost."""

    max_tokens: int = Field(default=2048, ge=1, le=4096)
    max_requests: int = Field(default=12, ge=1, le=40)
    max_request_bytes: int = Field(default=262144, ge=1024, le=1048576)
    input_usd_per_million: float = Field(gt=0)
    output_usd_per_million: float = Field(gt=0)
    max_cost_usd: float = Field(default=1, gt=0, le=5)
    capture_timeout_seconds: float = Field(default=540, gt=0, le=3600)
    """How long a captured (non-streamed) call waits for the engine's whole generation. The
    default stays under the 10-minute default timeout of the OpenAI SDK clients harnesses use."""

    @model_validator(mode="after")
    def bound_native_requests(self):
        # A request of n bytes holds at most n + 4096 input tokens, framing included.
        bound = (
            self.max_requests
            * (
                (self.max_request_bytes + 4096) * self.input_usd_per_million
                + self.max_tokens * self.output_usd_per_million
            )
            / 1_000_000
        )
        if bound > self.max_cost_usd:
            raise ValueError("native request limits exceed the cost bound")
        return self


def upstream_payload(provider: Provider, payload: JsonObject, max_tokens: int) -> JsonObject:
    """Bound the output of a Chat Completions request and apply the provider's dialect.

    OpenRouter takes `max_tokens` and its own `reasoning` and `usage` fields. A configured endpoint
    takes the standard `max_completion_tokens`, drops those OpenRouter-only fields, and, when the
    provider sets a reasoning effort, sends that effort in place of the harness's.
    """
    payload = {key: value for key, value in payload.items() if key != "max_completion_tokens"}
    if provider == "openrouter":
        return payload | {"max_tokens": max_tokens}
    payload = {
        key: value
        for key, value in payload.items()
        if key not in {"max_tokens", "reasoning", "usage"}
    }
    if provider.reasoning_effort is not None:
        payload["reasoning_effort"] = provider.reasoning_effort
    return payload | {"max_completion_tokens": max_tokens}


def responses_payload(provider: Provider, payload: JsonObject, max_tokens: int) -> JsonObject:
    """Bound the output of a Responses request; a provider's reasoning effort replaces the
    harness's."""
    payload = payload | {"max_output_tokens": max_tokens}
    if provider != "openrouter" and provider.reasoning_effort is not None:
        reasoning = payload.get("reasoning")
        payload["reasoning"] = (reasoning if isinstance(reasoning, dict) else {}) | {
            "effort": provider.reasoning_effort
        }
    return payload


def _hosted_tools(payload: JsonObject) -> list[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    kinds = (tool.get("type") if isinstance(tool, dict) else None for tool in tools)
    return sorted({str(kind) for kind in kinds if kind not in LOCAL_TOOL_TYPES})


def _media(value: JsonValue) -> bool:
    """Whether any part is an image, file or audio, which the provider bills beyond its bytes."""
    if isinstance(value, list):
        return any(_media(item) for item in value)
    if isinstance(value, dict):
        return value.get("type") in MEDIA_TYPES or any(_media(item) for item in value.values())
    return False


def _stored_context(payload: JsonObject) -> bool:
    items = payload.get("input")
    return isinstance(items, list) and any(
        isinstance(item, dict) and item.get("type") == "item_reference" for item in items
    )


def _usage(
    api: Api, message: JsonObject
) -> tuple[JsonValue, JsonValue, tuple[JsonValue, ...] | None]:
    """The generation ID, raw usage and (input, output, cost) of one streamed event."""
    if api == "chat":
        usage = message.get("usage")
        fields = usage if isinstance(usage, dict) else {}
        tokens = (fields.get("prompt_tokens"), fields.get("completion_tokens"), fields.get("cost"))
        return message.get("id"), usage, None if usage is None else tokens
    response = message.get("response")
    if not isinstance(response, dict):
        return None, None, None
    usage = response.get("usage")
    fields = usage if isinstance(usage, dict) else {}
    tokens = (fields.get("input_tokens"), fields.get("output_tokens"), fields.get("cost"))
    return response.get("id"), usage, None if usage is None else tokens


class HarnessLeft(Exception):
    """The harness disconnected before the engine answered its call."""


async def _unless_abandoned[T](request: Request, call: Coroutine[Any, Any, T]) -> T:
    """Await the engine `call`, and cancel it when the harness disconnects first.

    The server does not cancel a handler whose client left, so the call would run on and its
    late answer would look delivered to whoever serves the engine, for example a trainer that
    drops an episode with a call its harness gave up on."""

    async def left() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    answer = asyncio.ensure_future(call)
    gone = asyncio.ensure_future(left())
    try:
        await asyncio.wait([answer, gone], return_when=asyncio.FIRST_COMPLETED)
        if not answer.done():
            raise HarnessLeft
        return answer.result()
    finally:
        answer.cancel()
        gone.cancel()
        await asyncio.gather(answer, gone, return_exceptions=True)


class ProviderRelay:
    def __init__(
        self,
        budget: RequestBudget,
        provider: Provider,
        model: str,
        key: str,
        client: httpx.AsyncClient,
        record: Callable[[JsonObject], None],
        meter: UsageMeter | None = None,
        capture: Path | None = None,
    ):
        """`model` is the upstream model ID every request must name. With a `meter`, the relay
        reports usage for the process; without one the host meters it. With `capture`, every
        answered call's tokens are appended to that file (see the module docstring)."""
        self.budget = budget
        self.provider = provider
        self.model = model
        self.key = key
        self.client = client
        self.record = record
        self.meter = meter
        self.capture = capture
        self.token = secrets.token_urlsafe(32)
        self.requests = 0
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.completion, methods=["POST"]),
                Route("/v1/responses", self.response, methods=["POST"]),
            ]
        )

    async def completion(self, request: Request):
        return await self.relay(request, "chat")

    async def response(self, request: Request):
        return await self.relay(request, "responses")

    def reject(self, status: int, error: str, reason: str | None = None, **detail) -> JSONResponse:
        if reason is not None:
            self.record({"kind": "provider_rejected", "reason": reason, **detail})
        return JSONResponse({"error": error}, status_code=status)

    def refusal(self, api: Api, payload: JsonObject) -> JSONResponse | None:
        unsupported = sorted(set(payload) - ALLOWED_REQUEST_FIELDS[api])
        if unsupported:
            return self.reject(
                400,
                f"unsupported request fields: {', '.join(unsupported)}",
                "fields",
                fields=unsupported,
            )
        hosted = _hosted_tools(payload)
        if hosted:
            return self.reject(
                400, f"unsupported tool types: {', '.join(hosted)}", "tools", tools=hosted
            )
        if _media(payload.get("messages")) or _media(payload.get("input")):
            return self.reject(400, "only text content is supported", "media")
        if _stored_context(payload):
            return self.reject(400, "stored item references are unsupported", "stored_context")
        if payload.get("model") != self.model:
            return self.reject(400, "unexpected model")
        if self.capture is not None and api != "chat":
            _append(self.capture, UnansweredCall(request=None, reason="capture_api"))
            return self.reject(400, "capture supports chat completions only", "capture_api")
        return None

    async def relay(self, request: Request, api: Api):
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return self.reject(401, "unauthorized")
        body = await request.body()
        if len(body) > self.budget.max_request_bytes:
            return self.reject(
                413, "native request size limit exceeded", "request_bytes", bytes=len(body)
            )
        if self.requests >= self.budget.max_requests:
            return self.reject(429, "native request budget exhausted", "request_count")
        try:
            payload = request_contract.validate_json(body)
        except ValidationError:
            return self.reject(400, "request must be a JSON object")
        refused = self.refusal(api, payload)
        if refused is not None:
            return refused
        route = endpoint(self.provider)
        if api == "chat":
            payload = upstream_payload(self.provider, payload, self.budget.max_tokens)
            url = route.chat_completions_url
        else:
            payload = responses_payload(self.provider, payload, self.budget.max_tokens)
            url = f"{route.base_url}/responses"
        self.requests += 1
        request_number = self.requests
        if self.meter is not None:
            self.meter.begin()
        try:
            if self.capture is not None:
                return await self.captured(request, url, payload, request_number, self.capture)
            return await self.forwarded(request, url, payload, api, request_number)
        except HarnessLeft:
            self.record({"kind": "provider_abandoned", "request": request_number})
            # No one is left to read it.
            return Response(status_code=499)

    async def forwarded(
        self, request: Request, url: str, payload: JsonObject, api: Api, request_number: int
    ):
        try:
            upstream = await _unless_abandoned(
                request,
                self.client.send(
                    self.client.build_request(
                        "POST", url, json=payload, headers={"Authorization": f"Bearer {self.key}"}
                    ),
                    stream=True,
                ),
            )
        except BaseException:
            if self.meter is not None:
                self.meter.add_unmeasured()
            raise
        self.record(
            {"kind": "provider_request", "request": request_number, "status": upstream.status_code}
        )
        streamed = "text/event-stream" in upstream.headers.get("content-type", "")
        if not streamed and self.meter is not None:
            if upstream.is_client_error:
                self.meter.end()
            else:
                self.meter.add_unmeasured()
        return StreamingResponse(
            self.stream(upstream, api) if streamed else upstream.aiter_bytes(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            background=BackgroundTask(upstream.aclose),
        )

    async def stream(self, upstream: httpx.Response, api: Api):
        generation_id = None
        metered = False
        try:
            async for event in EventSource(upstream).aiter_sse():
                if event.data != "[DONE]":
                    message = request_contract.validate_json(event.data)
                    identifier, usage, tokens = _usage(api, message)
                    if identifier != generation_id and identifier is not None:
                        generation_id = identifier
                        self.record({"kind": "provider_generation", "id": generation_id})
                    if usage is not None:
                        self.record({"kind": "provider_usage", "id": generation_id, "usage": usage})
                    if tokens is not None and not metered:
                        metered = True
                        if self.meter is not None:
                            self.meter.add(*tokens)
                name = "" if event.event == "message" else f"event: {event.event}\n"
                yield f"{name}data: {event.data}\n\n"
        finally:
            if not metered and self.meter is not None:
                self.meter.add_unmeasured()

    async def captured(
        self, request: Request, url: str, payload: JsonObject, request_number: int, capture: Path
    ):
        """Call the engine once without streaming, store the call's tokens, answer the harness."""
        options = payload.get("stream_options")
        include_usage = isinstance(options, dict) and options.get("include_usage") is True
        streamed = payload.get("stream") is True
        upstream_request = {
            key: value for key, value in payload.items() if key not in {"stream", "stream_options"}
        } | {"stream": False, "logprobs": True, "return_token_ids": True}
        try:
            upstream = await _unless_abandoned(
                request,
                self.client.post(
                    url,
                    json=upstream_request,
                    headers={"Authorization": f"Bearer {self.key}"},
                    timeout=httpx.Timeout(
                        self.budget.capture_timeout_seconds, connect=self.client.timeout.connect
                    ),
                ),
            )
        except BaseException:
            # Failed, timed out, or abandoned by the harness before the engine answered.
            _append(capture, UnansweredCall(request=request_number, reason="engine_unreachable"))
            if self.meter is not None:
                self.meter.add_unmeasured()
            raise
        self.record(
            {"kind": "provider_request", "request": request_number, "status": upstream.status_code}
        )
        try:
            completion = request_contract.validate_json(upstream.content)
        except ValidationError:
            completion = None
        if not upstream.is_success or completion is None:
            reason = "engine_error" if not upstream.is_success else "no_completion"
            _append(
                capture,
                UnansweredCall(request=request_number, reason=reason, status=upstream.status_code),
            )
            return self.unanswered(upstream, completion)
        self.count(completion)
        call = _captured_call(request_number, completion)
        if call is None:
            _append(capture, UnansweredCall(request=request_number, reason="capture_missing"))
            return self.reject(
                502, "provider returned no token ids", "capture_missing", request=request_number
            )
        _append(capture, call)
        self.record(
            {
                "kind": "provider_captured",
                "request": request_number,
                "prompt_tokens": len(call.prompt_token_ids),
                "completion_tokens": len(call.completion_token_ids),
                "logprobs": call.logprobs,
            }
        )
        answer = _harness_completion(completion)
        if streamed:
            return StreamingResponse(
                _replayed_stream(answer, include_usage), media_type="text/event-stream"
            )
        return JSONResponse(answer)

    def unanswered(self, upstream: httpx.Response, body: JsonObject | None) -> JSONResponse:
        """Pass a provider error on, or refuse a success that carried no completion."""
        if self.meter is not None:
            if upstream.is_client_error:
                self.meter.end()
            else:
                self.meter.add_unmeasured()
        if upstream.is_success:
            return self.reject(502, "provider answered without a completion")
        return JSONResponse(
            body if body is not None else {"error": "provider error"},
            status_code=upstream.status_code,
        )

    def count(self, completion: JsonObject) -> None:
        """Record and meter one non-streamed completion."""
        identifier, usage, tokens = _usage("chat", completion)
        if identifier is not None:
            self.record({"kind": "provider_generation", "id": identifier})
        if usage is not None:
            self.record({"kind": "provider_usage", "id": identifier, "usage": usage})
        if self.meter is not None:
            if tokens is None:
                self.meter.add_unmeasured()
            else:
                self.meter.add(*tokens)
