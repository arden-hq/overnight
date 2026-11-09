import socket
from contextlib import asynccontextmanager, nullcontext

import anyio
import uvicorn
from starlette.applications import Starlette


class _OwnedServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


@asynccontextmanager
async def serve_http(app: Starlette, port: int = 0):
    """Serve on loopback only; port 0 picks a free port."""
    with socket.socket() as sock:
        if port:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen()
        sock.setblocking(False)
        server = _OwnedServer(uvicorn.Config(app, log_level="error", lifespan="off"))
        stopped = anyio.Event()

        async def run_server():
            try:
                await server.serve([sock])
            finally:
                stopped.set()

        async with anyio.create_task_group() as group:
            group.start_soon(run_server)
            with anyio.fail_after(10):
                while not server.started:
                    await anyio.sleep(0.01)
            try:
                yield f"http://127.0.0.1:{sock.getsockname()[1]}"
            finally:
                server.should_exit = True
                with anyio.move_on_after(5):
                    await stopped.wait()
                group.cancel_scope.cancel()
