import asyncio
import contextlib
import json
import os
import signal
import tempfile
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import verifiers.v1 as vf
from pydantic import Field

from overnight_verifiers.forwarder import PolicyForwarder
from overnight_verifiers.taskset import (
    UNANSWERED_WHEN_CANCELLED,
    OvernightError,
    engine_fault,
    has_persona,
    overnight_python,
)

POLICY_KEY = "OVERNIGHT_POLICY_KEY"
OPERATOR_KEY = "OVERNIGHT_OPERATOR_KEY"
INTERRUPT_GRACE_SECONDS = 20.0


@dataclass(frozen=True)
class Connection:
    endpoint: str
    secret: str
    model: str


_SESSIONS_BY_ROLE: ContextVar[dict[str, Connection]] = ContextVar("overnight_episode_sessions")


@contextlib.contextmanager
def own_episode_sessions() -> Generator[None, None, None]:
    token = _SESSIONS_BY_ROLE.set({})
    try:
        yield
    finally:
        _SESSIONS_BY_ROLE.reset(token)


class OvernightHarnessConfig(vf.HarnessConfig):
    python: str = Field(default_factory=overnight_python)
    """Overnight's interpreter. Default: `OVERNIGHT_PYTHON`."""
    agent: Path | None = None
    """An Overnight agent document (JSON) that names the policy with `{policy.base_url}`,
    `{policy.api_key_env}` and `{policy.model}`. None keeps each case's own agent and uses no
    policy, which only smoke-tests the wiring."""
    root: Path | None = None
    """Folder for episode folders, one per rollout. Default: a new temporary folder."""
    keep: bool = False
    """Keep each episode's folder under `root`."""
    operator_model: dict[str, Any] = Field(default_factory=dict)
    """Bounds for Overnight's operator model (`max_calls`, `max_cost_usd`, ...). The operator's
    endpoint and model always come from the env's `operator` session."""


class OvernightHarness(vf.Harness[OvernightHarnessConfig]):
    """Runs one Overnight episode per rollout through `overnight.adapters.rollout`. The worker
    is a host process: Overnight decides whether the agent runs in a container."""

    NEEDS_CONTAINER = False

    def __init__(self, config: OvernightHarnessConfig) -> None:
        super().__init__(config)
        self._agent: dict[str, Any] | None = (
            json.loads(config.agent.read_text(encoding="utf-8"))
            if config.agent is not None
            else None
        )
        self._root = config.root or Path(tempfile.mkdtemp(prefix="overnight-verifiers-"))

    async def session(self, ctx, trace, runtime, endpoint, secret, mcp_urls, data, **kwargs):
        sessions = _SESSIONS_BY_ROLE.get(None)
        if sessions is not None and trace.agent is not None and trace.agent.name:
            sessions[trace.agent.name] = Connection(endpoint, secret, ctx.model)
        return await super().session(
            ctx, trace, runtime, endpoint, secret, mcp_urls, data, **kwargs
        )

    def _request(
        self,
        episode: str,
        scenario: dict[str, Any],
        policy: Connection,
        operator: Connection | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"id": episode, "scenario": scenario, "capture": False}
        if self._agent is not None:
            request["agent"] = self._agent
            request["policy"] = {
                "provider": {"base_url": policy.endpoint, "api_key_env": POLICY_KEY},
                "model": policy.model,
            }
        if has_persona(scenario):
            if operator is None:
                raise OvernightError("a persona case needs the env's operator session")
            request["operator_model"] = {
                **self.config.operator_model,
                "provider": {"base_url": operator.endpoint, "api_key_env": OPERATOR_KEY},
                "model": operator.model,
            }
            # On the worker's stdin, never in its environment: a host agent can read the
            # environment its ancestors started with (`ps eww`), but not their input.
            request["operator_key"] = operator.secret
        return request

    async def launch(
        self, ctx, trace, runtime, endpoint, secret, mcp_urls, data
    ) -> vf.ProgramResult:
        if not self.config.python:
            raise OvernightError(
                "set OVERNIGHT_PYTHON or harness.python to Overnight's interpreter"
            )
        operator = _SESSIONS_BY_ROLE.get({}).get("operator")
        forwarder = PolicyForwarder(endpoint)
        async with forwarder.serving() as forwarded:
            policy = Connection(forwarded, secret, ctx.model)
            request = self._request(trace.id, data.scenario, policy, operator)
            environ = os.environ | self.config.resolved_env | {POLICY_KEY: secret}
            kept_in_clean_env = [POLICY_KEY, *self.config.resolved_env]
            result = await self._run_worker(
                json.dumps(request), environ, kept_in_clean_env, trace, forwarder
            )
            unanswered = forwarder.unanswered
        if unanswered:
            raise OvernightError(
                f"the episode ended with {unanswered} policy call(s) the harness got no answer to"
            )
        if "error" in result:
            raise OvernightError(f"Overnight refused the episode: {result['error']}")
        failure = result["failure"]
        if failure is not None and failure["kind"] == "infrastructure":
            raise OvernightError(f"Overnight infrastructure failure: {failure}")
        fault = engine_fault(trace)
        if fault is not None:
            raise OvernightError(f"the model engine failed under the policy: {fault}")
        trace.info["overnight"] = {
            "episode": result["id"],
            "scenario_id": result["scenario_id"],
            "verdict": result["verdict"],
            "score": result["score"],
            "failure": failure,
            "policy_model": result["policy_model"],
            "usage": result["usage"],
            "operator_usage": result["operator_usage"],
        }
        return vf.ProgramResult(exit_code=0, stdout="", stderr="")

    async def _run_worker(
        self,
        line: str,
        environ: dict[str, str],
        kept_in_clean_env: list[str],
        trace: vf.Trace,
        forwarder: PolicyForwarder,
    ) -> dict[str, Any]:
        command = [
            self.config.python,
            "-m",
            "overnight.adapters.rollout",
            "--root",
            str(self._root),
            "--clean-env",
        ]
        for name in kept_in_clean_env:
            command += ["--pass-env", name]
        if self.config.keep:
            command.append("--keep")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environ,
            start_new_session=True,
        )
        try:
            stdout, stderr = await process.communicate(line.encode() + b"\n")
        except BaseException:
            unanswered_before_stop = forwarder.unanswered
            trace.info[UNANSWERED_WHEN_CANCELLED] = unanswered_before_stop
            await asyncio.shield(_interrupt_then_kill_group(process))
            raise
        lines = stdout.decode().strip().splitlines()
        if process.returncode != 0 or not lines:
            detail = stderr.decode().strip()[-2000:] or "<no output>"
            raise OvernightError(
                f"Overnight's rollout worker exited {process.returncode}: {detail}"
            )
        return json.loads(lines[-1])


async def _interrupt_then_kill_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(process.wait(), INTERRUPT_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
