import hashlib
from pathlib import Path
from typing import Literal, cast

from acp.core import ClientSideConnection
from pydantic import BaseModel, ConfigDict, Field

from overnight.adapters.config import NativeObservation, NativeRequest, NativeTurn, SessionBinding
from overnight.domain.contracts import JsonObject
from overnight.execution.harness import HarnessStatus
from overnight_prime.completion import validate_completion

EPOCH_MS = 1_735_689_600_000


class EvaluationBuild(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    commit: Literal["763094e1b6a7c17450ee8399b06cda1853847ca5"]
    patch_sha256: str
    binary_sha256: str
    toolchain: str
    profile: str


def require_evaluation_build(runtime: Path) -> None:
    manifest = runtime / "eval-build.json"
    if not manifest.is_file():
        raise ValueError(
            "build the scoped Prime evaluation runtime with connectors/prime/build_native.py"
        )
    build = EvaluationBuild.model_validate_json(manifest.read_bytes())
    patch = Path(__file__).with_name("native-eval.patch")
    if build.patch_sha256 != hashlib.sha256(patch.read_bytes()).hexdigest():
        raise ValueError("Prime evaluation runtime uses a different native patch")
    if (
        build.binary_sha256
        != hashlib.sha256((runtime / "bin/prime-agent").read_bytes()).hexdigest()
    ):
        raise ValueError("Prime native binary differs from its recorded evaluation build")


class SchedulerState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    now_ms: int = Field(alias="nowMs", ge=0)
    active_work: bool = Field(alias="activeWork", default=False)
    next_wake_ms: int | None = Field(alias="nextWakeMs", default=None)


class NativeLifecycle:
    def __init__(self, clock: Path):
        self.clock = clock
        self.time = 0
        self.connection: ClientSideConnection | None = None
        self.session_id: str | None = None
        self.write_clock(0)

    def write_clock(self, time: int) -> None:
        temporary = self.clock.with_suffix(".next")
        temporary.write_text(str(EPOCH_MS + time * 1000))
        temporary.chmod(0o600)
        temporary.replace(self.clock)
        self.time = time

    async def on_session(self, binding: SessionBinding) -> None:
        self.connection = cast(ClientSideConnection, binding.connection)
        self.session_id = binding.session_id
        await self.status()

    async def status(self) -> NativeObservation:
        assert self.connection is not None
        response = await self.connection.ext_method("overnight/status", {})
        state = SchedulerState.model_validate(response["overnightEval"])
        if state.now_ms != EPOCH_MS + self.time * 1000:
            raise RuntimeError("Prime evaluation clock did not advance to world time")
        wake = state.next_wake_ms
        next_wake = None if wake is None else max(self.time, (wake - EPOCH_MS + 999) // 1000)
        request = None
        if state.active_work or (wake is not None and wake <= state.now_ms):
            next_wake = None
            request = NativeRequest(
                method="overnight/advance", params={"sessionId": self.session_id}
            )
        return NativeObservation(
            status=HarnessStatus(
                pending=state.active_work or wake is not None,
                busy=state.active_work,
                next_wake=next_wake,
            ),
            request=request,
        )

    async def observe(self, observation: JsonObject) -> NativeObservation:
        time = observation["time"]
        if not isinstance(time, int) or isinstance(time, bool):
            raise ValueError("world time must be an integer")
        if time < self.time:
            raise ValueError("world time cannot move backwards without reset")
        self.write_clock(time)
        return await self.status()

    async def after_turn(self, turn: NativeTurn) -> NativeObservation:
        validate_completion(turn)
        return await self.status()

    async def cancel(self) -> None:
        if self.connection is None:
            return
        await self.connection.ext_method("overnight/cancel", {})

    async def reset(self) -> None:
        assert self.connection is not None and self.session_id is not None
        await self.connection.close_session(session_id=self.session_id)
        await self.connection.ext_method("overnight/reset", {})
