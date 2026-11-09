import secrets
from collections.abc import Callable

import httpx
from httpx_sse import EventSource
from pydantic import TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from overnight.adapters.evidence import UsageMeter
from overnight.adapters.provider import Provider, endpoint
from overnight.domain.contracts import JsonObject
from overnight_prime.options import PrimeOptions

request_contract = TypeAdapter(JsonObject)

# Standard chat-completion fields only. OpenRouter routing and spend controls such as `models`,
# `provider`, `plugins`, `route`, `transforms` and `n` could bypass the pinned model and budget.
ALLOWED_REQUEST_FIELDS = frozenset(
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
)


def upstream_payload(provider: Provider, payload: JsonObject, max_tokens: int) -> JsonObject:
    """Bound the output and apply the provider's request dialect.

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


class ProviderRelay:
    def __init__(
        self,
        options: PrimeOptions,
        key: str,
        client: httpx.AsyncClient,
        record: Callable[[JsonObject], None],
    ):
        self.options = options
        self.key = key
        self.client = client
        self.record = record
        self.meter = UsageMeter()
        self.token = secrets.token_urlsafe(32)
        self.requests = 0
        self.app = Starlette(
            routes=[Route("/v1/chat/completions", self.completion, methods=["POST"])]
        )

    async def completion(self, request: Request):
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.body()
        if len(body) > self.options.max_request_bytes:
            self.record(
                {"kind": "provider_rejected", "reason": "request_bytes", "bytes": len(body)}
            )
            return JSONResponse({"error": "native request size limit exceeded"}, status_code=413)
        if self.requests >= self.options.max_requests:
            self.record({"kind": "provider_rejected", "reason": "request_count"})
            return JSONResponse({"error": "native request budget exhausted"}, status_code=429)
        try:
            payload = request_contract.validate_json(body)
        except ValidationError:
            return JSONResponse({"error": "request must be a JSON object"}, status_code=400)
        unsupported = sorted(set(payload) - ALLOWED_REQUEST_FIELDS)
        if unsupported:
            self.record({"kind": "provider_rejected", "reason": "fields", "fields": unsupported})
            return JSONResponse(
                {"error": f"unsupported request fields: {', '.join(unsupported)}"},
                status_code=400,
            )
        if payload.get("model") != self.options.model:
            return JSONResponse({"error": "unexpected model"}, status_code=400)
        payload = upstream_payload(self.options.provider, payload, self.options.max_tokens)
        self.requests += 1
        request_number = self.requests
        self.meter.begin()
        try:
            upstream = await self.client.send(
                self.client.build_request(
                    "POST",
                    endpoint(self.options.provider).chat_completions_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.key}"},
                ),
                stream=True,
            )
        except BaseException:
            self.meter.add_unmeasured()
            raise
        self.record(
            {"kind": "provider_request", "request": request_number, "status": upstream.status_code}
        )
        if "text/event-stream" not in upstream.headers.get("content-type", ""):
            if upstream.is_client_error:
                self.meter.end()
            else:
                self.meter.add_unmeasured()
        return StreamingResponse(
            self.stream(upstream)
            if "text/event-stream" in upstream.headers.get("content-type", "")
            else upstream.aiter_bytes(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            background=BackgroundTask(upstream.aclose),
        )

    async def stream(self, upstream: httpx.Response):
        generation_id = None
        metered = False
        try:
            async for event in EventSource(upstream).aiter_sse():
                if event.data != "[DONE]":
                    message = request_contract.validate_json(event.data)
                    if message.get("id") != generation_id and message.get("id") is not None:
                        generation_id = message["id"]
                        self.record({"kind": "provider_generation", "id": generation_id})
                    usage = message.get("usage")
                    if usage is not None:
                        self.record({"kind": "provider_usage", "id": generation_id, "usage": usage})
                    if usage is not None and not metered:
                        metered = True
                        fields = usage if isinstance(usage, dict) else {}
                        self.meter.add(
                            fields.get("prompt_tokens"),
                            fields.get("completion_tokens"),
                            fields.get("cost"),
                        )
                yield f"data: {event.data}\n\n"
        finally:
            if not metered:
                self.meter.add_unmeasured()
