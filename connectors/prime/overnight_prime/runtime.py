import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from overnight.domain.contracts import JsonObject


def native_environment(environment: dict[str, str]) -> dict[str, str]:
    inherited = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    return inherited | environment


class NativeRuntime:
    def __init__(
        self,
        binary: Path,
        workspace: Path,
        environment: dict[str, str],
        directory: Path,
        record: Callable[[JsonObject], None],
    ):
        self.binary = binary
        self.workspace = workspace
        self.environment = environment
        self.directory = directory
        self.record = record
        self.daemon: asyncio.subprocess.Process | None = None

    async def start(self, socket: Path) -> None:
        with (self.directory / "daemon.log").open("xb") as log:
            os.chmod(log.name, 0o600)
            self.daemon = await asyncio.create_subprocess_exec(
                str(self.binary),
                "--mode",
                "daemon",
                "--daemon-socket",
                str(socket),
                env=self.environment,
                cwd=self.workspace,
                stdout=log,
                stderr=log,
            )
        try:
            async with asyncio.timeout(10):
                while not socket.exists():
                    if self.daemon.returncode is not None:
                        raise RuntimeError("Prime daemon failed before startup")
                    await asyncio.sleep(0.05)
        except BaseException:
            await self.close()
            raise
        self.record({"kind": "owned_daemon_started", "pid": self.daemon.pid})

    async def close(self) -> None:
        if self.daemon is None:
            return
        daemon, self.daemon = self.daemon, None
        if daemon.returncode is None:
            daemon.terminate()
            try:
                async with asyncio.timeout(5):
                    await daemon.wait()
            except TimeoutError:
                daemon.kill()
                await daemon.wait()
        self.record(
            {"kind": "owned_daemon_closed", "pid": daemon.pid, "exit_code": daemon.returncode}
        )
        shutdown = await asyncio.create_subprocess_exec(
            str(self.binary),
            "shutdown",
            "--force",
            env=self.environment,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(10):
                stdout, stderr = await shutdown.communicate()
        except BaseException:
            if shutdown.returncode is None:
                shutdown.kill()
            await shutdown.wait()
            raise
        self.record(
            {
                "kind": "prime_shutdown",
                "exit_code": shutdown.returncode,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            }
        )
        if shutdown.returncode != 0:
            raise RuntimeError("Prime native shutdown failed")
