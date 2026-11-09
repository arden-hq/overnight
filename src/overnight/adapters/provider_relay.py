"""A loopback relay that holds the provider key and bounds a native harness's paid requests.

The native process gets the relay's URL and a per-run token instead of the key. The relay forwards
Chat Completions (`/v1/chat/completions`) and Responses (`/v1/responses`) requests, streaming or
not, to the configured provider. It refuses, before anything is sent upstream, a request over the
byte limit, any request after `max_requests` were sent, request fields outside a standard set,
hosted tools, image, file or audio parts, references to provider-stored context and an unexpected model. It replaces the
output bound with `max_tokens`. `RequestBudget` checks that these limits fit `max_cost_usd` at the
caller's rates, so the cost bound holds before money is spent; it is not provider billing.

Each decision is recorded through `record`; the key is never recorded.
"""

import secrets
from collections.abc import Callable
from typing import Literal

import httpx
from httpx_sse import EventSource
from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from overnight.adapters.evidence import UsageMeter
from overnight.adapters.provider import Provider, endpoint
from overnight.domain.contracts import Contract, JsonObject

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


class RequestBudget(Contract):
    """Per-run request limits and the rates that bound their cost."""

    max_tokens: int = Field(default=2048, ge=1, le=4096)
    max_requests: int = Field(default=12, ge=1, le=40)
    max_request_bytes: int = Field(default=262144, ge=1024, le=1048576)
    input_usd_per_million: float = Field(gt=0)
    output_usd_per_million: float = Field(gt=0)
    max_cost_usd: float = Field(default=1, gt=0, le=5)

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
    ):
        """`model` is the upstream model ID every request must name. With a `meter`, the relay
        reports usage for the process; without one the host meters it."""
        self.budget = budget
        self.provider = provider
        self.model = model
        self.key = key
        self.client = client
        self.record = record
        self.meter = meter
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
            upstream = await self.client.send(
                self.client.build_request(
                    "POST", url, json=payload, headers={"Authorization": f"Bearer {self.key}"}
                ),
                stream=True,
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
