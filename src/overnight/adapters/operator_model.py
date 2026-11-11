import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from http.client import HTTPMessage
from typing import IO, Literal

from overnight.adapters.provider import endpoint
from overnight.domain.contracts import OperatorModel
from overnight.domain.values import JsonObject
from overnight.execution.operator import Completion, Expects, Message, OperatorUnavailable
from overnight.execution.usage import ProcessUsage, readable

MAX_RESPONSE_BYTES = 1_048_576


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the key to a host nobody configured, so it ends the call."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise OperatorUnavailable(f"operator model redirected (HTTP {code}); not followed")


_opener = urllib.request.build_opener(_RefuseRedirects)


class ModelTransport:
    source: Literal["model"] = "model"

    def __init__(
        self, config: OperatorModel, seed: int, environ: Mapping[str, str] = os.environ
    ) -> None:
        provider = endpoint(config.provider)
        key = environ.get(provider.api_key_env)
        if not key:
            raise OperatorUnavailable(f"operator model key {provider.api_key_env} is not set")
        self.config = config
        self.url = provider.chat_completions_url
        self._key = key
        self.settings: JsonObject = {
            "base_url": provider.base_url,
            "model": config.model,
            "max_completion_tokens": config.max_output_tokens,
            "temperature": 0,
            "seed": seed,
        }
        if provider.reasoning_effort is not None:
            self.settings["reasoning_effort"] = provider.reasoning_effort

    def complete(self, prompt: Sequence[Message], expects: Expects) -> Completion:
        body = {key: value for key, value in self.settings.items() if key != "base_url"}
        request = urllib.request.Request(
            self.url,
            data=json.dumps({**body, "messages": list(prompt)}).encode(),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with _opener.open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise OperatorUnavailable(
                f"operator model returned HTTP {error.code}: {self._provider_error(error)}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OperatorUnavailable(
                f"operator model unreachable: {type(error).__name__}"
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OperatorUnavailable("operator model response exceeds 1 MiB")
        try:
            reply = json.loads(raw)
            text = reply["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("content is not text")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OperatorUnavailable(f"operator model response unreadable: {error}") from None
        return Completion(
            text=text,
            usage=self._usage(reply.get("usage")),
            model=reply.get("model") if isinstance(reply.get("model"), str) else None,
            settings=self.settings,
        )

    def _usage(self, usage: object) -> ProcessUsage:
        counts = (
            readable(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
            if isinstance(usage, dict)
            else {}
        )
        input_tokens, output_tokens = counts.get("input_tokens"), counts.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise OperatorUnavailable(
                "operator model reported no token usage, so its cost cannot be bounded"
            )
        cost = (
            input_tokens * self.config.input_usd_per_million
            + output_tokens * self.config.output_usd_per_million
        ) / 1_000_000
        return ProcessUsage.model_validate(
            {"calls": 1, **counts, **readable(cost_usd=cost)}, strict=False
        )

    def _provider_error(self, error: urllib.error.HTTPError) -> str:
        try:
            detail = json.loads(error.read(65_536))
            message = detail.get("error", {}).get("message") if isinstance(detail, dict) else None
        except ValueError, OSError, AttributeError:
            message = None
        text = message if isinstance(message, str) else str(error.reason or "no detail")
        return text.replace(self._key, "[key]")[:300]

    def finish(self) -> None:
        pass
