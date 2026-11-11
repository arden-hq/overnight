"""A loopback forwarder in front of a rollout's interception endpoint that counts the policy calls
the harness did not get an answer to.

verifiers' interception server keeps a model turn running when its reader disconnects, so a
retry can coalesce onto it, and records the turn as answered once the engine answers. A call the
harness gave up on, for example at its client's timeout, then looks answered on the trace. The
forwarder sees the harness's side: a call is answered only when its whole answer was written
before the harness left.
"""

import contextlib
import socket
from collections.abc import AsyncIterator

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

# verifiers' own request limit (`MAX_REQUEST_BODY`); aiohttp's default is 1 MiB.
MAX_REQUEST_BODY = 1024**3
SKIPPED_HEADERS = frozenset(
    {
        "connection",
        "proxy-connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class PolicyForwarder:
    def __init__(self, endpoint: str) -> None:
        self._endpoint = URL(endpoint)
        self._client: aiohttp.ClientSession | None = None
        self.open = 0
        self.abandoned = 0

    @property
    def unanswered(self) -> int:
        return self.open + self.abandoned

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator[str]:
        app = web.Application(client_max_size=MAX_REQUEST_BODY)
        app.router.add_route("*", "/{path:.*}", self._forward)
        # aiohttp cancels a handler when its client disconnects only with handler_cancellation.
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=1)
        await runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            async with aiohttp.ClientSession(
                auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None)
            ) as client:
                self._client = client
                await web.SockSite(runner, sock).start()
                yield str(self._endpoint.with_scheme("http").with_host("127.0.0.1").with_port(port))
        finally:
            await runner.cleanup()
            sock.close()

    async def _forward(self, request: web.Request) -> web.StreamResponse:
        assert self._client is not None
        call = request.method == "POST"
        answered = False
        self.open += call
        try:
            body = await request.read()
            async with self._client.request(
                request.method,
                URL(str(self._endpoint.origin()) + request.raw_path, encoded=True),
                # aiohttp hands the handler a decoded body.
                headers=_forwarded(request.headers, {"content-encoding"}),
                data=body,
                allow_redirects=False,
            ) as upstream:
                response = web.StreamResponse(
                    status=upstream.status, headers=_forwarded(upstream.headers)
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                answered = True
                return response
        finally:
            self.open -= call
            if call and not answered:
                self.abandoned += 1


def _forwarded(headers, skipped: set[str] | None = None) -> CIMultiDict[str]:
    skipped = SKIPPED_HEADERS | (skipped or set())
    return CIMultiDict(
        (name, value) for name, value in headers.items() if name.lower() not in skipped
    )
