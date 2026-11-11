import asyncio
import contextlib
import json
import os
import signal
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from secrets import token_hex
from typing import Any
from uuid import uuid4

import tinker
from aiohttp import web
from tinker_cookbook import model_info, renderers
from tinker_cookbook.capture.proxy.app import ProxyDeps, make_app
from tinker_cookbook.completers import StopCondition
from tinker_cookbook.rl.rollout_limits import RolloutLimits
from tinker_cookbook.rl.types import (
    ActionExtra,
    Env,
    EnvGroupBuilder,
    InitialObservationOverflow,
    Observation,
    StepResult,
)
from tinker_cookbook.tokenizer_utils import get_tokenizer

from overnight_tinker.cases import Case, has_persona

POLICY_KEY = "OVERNIGHT_POLICY_KEY"
STOP_SECONDS = 20.0


class OvernightError(Exception):
    """Overnight could not run or grade the episode, or the proxy refused the harness. The
    episode says nothing about the policy, so it is raised: a cookbook rollout strategy that
    tolerates errors (`RetryOnFailure`, `MinViableGroup`) drops it."""


@dataclass(frozen=True)
class EpisodeConfig:
    python: str
    """Overnight's interpreter."""
    model_name: str
    """The trained base model: its tokenizer, and the `{policy.model}` the agent is given."""
    operator_model: dict[str, Any]
    """Overnight's operator model for persona cases, with a compatible `provider` whose
    `api_key_env` the trainer's environment holds. The key is sent on the worker's input."""
    agent: dict[str, Any] | None = None
    """The Overnight agent document that names `{policy.base_url}`, `{policy.api_key_env}` and
    `{policy.model}`. None keeps each case's own agent and uses no policy."""
    renderer_name: str | None = None
    root: str | None = None
    keep: bool = False
    pass_env: tuple[str, ...] = ()
    """Variables of the trainer's environment the worker keeps beside its own allow list."""
    rollout_timeout_seconds: float | None = None
    """The episode's deadline. Past it, an episode waiting on its agent scores 0, and one whose
    policy call is still with the sampler is dropped."""
    sampling_turn_timeout_seconds: float | None = None
    """The cookbook's bound on one sampling call; past it the episode is dropped. Default: the
    rollout timeout, so a stalled sampler cannot hold an episode forever."""


def operator_key(config: EpisodeConfig) -> str:
    provider = config.operator_model.get("provider")
    if not isinstance(provider, dict) or not isinstance(provider.get("api_key_env"), str):
        raise ValueError(
            "the operator model needs a compatible provider: "
            '{"provider": {"base_url": ..., "api_key_env": ...}, "model": ...}'
        )
    name = provider["api_key_env"]
    if name not in os.environ:
        raise ValueError(f"the operator model's key variable {name} is not set")
    return os.environ[name]


@cache
def _renderer(model_name: str, renderer_name: str | None) -> renderers.Renderer:
    name = renderer_name or model_info.get_recommended_renderer_name(model_name)
    return renderers.get_renderer(name, get_tokenizer(model_name), model_name=model_name)


@cache
def _default_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="overnight-tinker-"))


@dataclass(frozen=True)
class _Sequence:
    tokens: list[int]
    stop_reason: str


@dataclass(frozen=True)
class _Response:
    sequences: list[_Sequence]


@dataclass
class _Pending:
    prompt: tinker.ModelInput
    stop: StopCondition
    answer: asyncio.Future[_Response]


@dataclass
class _Bridge:
    requests: asyncio.Queue[_Pending] = field(default_factory=asyncio.Queue)

    async def sample(
        self, prompt: tinker.ModelInput, num_samples: int, sampling_params: tinker.SamplingParams
    ) -> _Response:
        stop = list(sampling_params.stop or [])
        pending = _Pending(prompt, stop, asyncio.get_running_loop().create_future())
        await self.requests.put(pending)
        return await pending.answer


def episode_request(
    config: EpisodeConfig, case: Case, episode: str, base_url: str
) -> dict[str, Any]:
    request: dict[str, Any] = {"id": episode, "scenario": case.scenario, "capture": False}
    if config.agent is not None:
        request["agent"] = config.agent
        request["policy"] = {
            "provider": {"base_url": base_url, "api_key_env": POLICY_KEY},
            "model": config.model_name,
        }
    if has_persona(case.scenario):
        request["operator_model"] = config.operator_model
        # On the worker's input, never in its environment, which a host agent can read.
        request["operator_key"] = operator_key(config)
    return request


def reward(result: dict[str, Any]) -> tuple[float, dict[str, float]]:
    if "error" in result:
        raise OvernightError(f"Overnight refused the episode: {result['error']}")
    failure = result["failure"]
    metrics = {"overnight/passed": float(result["verdict"] == "passed")}
    if failure is not None and failure["kind"] == "agent":
        return 0.0, metrics | {"overnight/agent_failure": 1.0}
    if failure is not None:
        raise OvernightError(f"Overnight infrastructure failure: {failure}")
    if result["score"] is None:
        raise OvernightError(f"Overnight returned no score (verdict {result['verdict']})")
    return float(result["score"]), metrics | {"overnight/agent_failure": 0.0}


class OvernightEnv(Env):
    def __init__(self, config: EpisodeConfig, case: Case) -> None:
        self.config = config
        self.case = case
        self.episode = uuid4().hex
        sampling = config.sampling_turn_timeout_seconds or config.rollout_timeout_seconds
        if sampling is not None:
            # Read by the cookbook's rollout loop: a sampling call past it raises, and the
            # episode is dropped. The episode's own deadline is this env's (`_next`), so a
            # deadline that falls while the sampler works is never scored.
            self.rollout_limits = RolloutLimits(sampling_turn_timeout_seconds=sampling)
        self.result: dict[str, Any] | None = None
        self._bridge = _Bridge()
        self._current: _Pending | None = None
        self._proxy_errors: list[str] = []
        self._deadline: float | None = None
        self._timed_out = False
        self._runner: web.AppRunner | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._worker: asyncio.Task[dict[str, Any]] | None = None
        self._closing: asyncio.Task[None] | None = None

    async def initial_observation(
        self,
    ) -> tuple[Observation, StopCondition] | InitialObservationOverflow:
        try:
            await self._start()
            pending = await self._next()
            if pending is None:
                score, metrics = self._final()
                return InitialObservationOverflow(score, metrics)
            return pending.prompt, pending.stop
        except BaseException:
            self.close_soon()
            raise

    async def step(self, action: list[int], *, extra: ActionExtra | None = None) -> StepResult:
        try:
            assert self._current is not None
            if self._current.answer.done():
                raise OvernightError("the harness gave up on a policy call before it was sampled")
            if self._past_deadline():
                raise OvernightError("the episode's deadline passed while the sampler worked")
            stop_reason = (extra or {}).get("stop_reason", "stop")
            self._current.answer.set_result(_Response([_Sequence(action, stop_reason)]))
            pending = await self._next()
            if pending is not None:
                return StepResult(0.0, False, pending.prompt, pending.stop)
            score, metrics = self._final()
            return StepResult(score, True, tinker.ModelInput.empty(), [], metrics)
        except BaseException:
            self.close_soon()
            raise

    async def _start(self) -> None:
        renderer = _renderer(self.config.model_name, self.config.renderer_name)
        secret = token_hex(16)
        deps = ProxyDeps(renderer=renderer, sampling_client=self._bridge, model_label="policy")
        app = make_app(deps, auth_token=secret)
        app.middlewares.insert(0, self._watch)
        self._runner = web.AppRunner(app, handler_cancellation=True)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        _host, port = self._runner.addresses[0][:2]
        request = episode_request(
            self.config, self.case, self.episode, f"http://127.0.0.1:{port}/v1"
        )
        if self.config.rollout_timeout_seconds is not None:
            loop = asyncio.get_running_loop()
            self._deadline = loop.time() + self.config.rollout_timeout_seconds
        environ = os.environ | {POLICY_KEY: secret}
        self._worker = asyncio.create_task(self._run_worker(json.dumps(request), environ))

    def _past_deadline(self) -> bool:
        return self._deadline is not None and asyncio.get_running_loop().time() >= self._deadline

    @web.middleware
    async def _watch(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            response = await handler(request)
        except (Exception, asyncio.CancelledError) as error:
            self._proxy_errors.append(f"{type(error).__name__}: {error}")
            raise
        if response.status >= 400:
            body = response.text if isinstance(response, web.Response) else None
            self._proxy_errors.append(f"{response.status} {body}")
        return response

    async def _next(self) -> _Pending | None:
        assert self._worker is not None
        getter = asyncio.ensure_future(self._bridge.requests.get())
        remaining = None
        if self._deadline is not None:
            remaining = max(0.0, self._deadline - asyncio.get_running_loop().time())
        try:
            await asyncio.wait(
                {getter, self._worker}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not getter.done():
                getter.cancel()
            elif not getter.cancelled():
                self._current = getter.result()
        if self._worker.done():
            self.result = self._worker.result()
            return None
        if getter.done() and not getter.cancelled():
            return self._current
        self._timed_out = True
        self.close_soon()
        return None

    def _final(self) -> tuple[float, dict[str, float]]:
        if self._proxy_errors:
            raise OvernightError(f"the policy proxy failed a harness call: {self._proxy_errors[0]}")
        if self._timed_out:
            return 0.0, {"overnight/agent_failure": 0.0, "stop/rollout_timeout": 1.0}
        assert self.result is not None
        score, metrics = reward(self.result)
        return score, metrics | {"stop/completed": 1.0}

    async def _run_worker(self, line: str, environ: dict[str, str]) -> dict[str, Any]:
        root = Path(self.config.root) if self.config.root else _default_root()
        command = [self.config.python, "-m", "overnight.adapters.rollout", "--root", str(root)]
        command += ["--clean-env", "--pass-env", POLICY_KEY]
        for name in self.config.pass_env:
            command += ["--pass-env", name]
        if self.config.keep:
            command.append("--keep")
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environ,
            start_new_session=True,
        )
        try:
            stdout, stderr = await self._process.communicate(line.encode() + b"\n")
        except BaseException:
            await asyncio.shield(_stop(self._process))
            raise
        lines = stdout.decode().strip().splitlines()
        if self._process.returncode != 0 or not lines:
            detail = stderr.decode().strip()[-2000:] or "<no output>"
            raise OvernightError(
                f"Overnight's rollout worker exited {self._process.returncode}: {detail}"
            )
        return json.loads(lines[-1])

    def close_soon(self) -> None:
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._close())

    async def close(self) -> None:
        self.close_soon()
        assert self._closing is not None
        await asyncio.shield(self._closing)

    async def _close(self) -> None:
        if self._process is not None:
            await _stop(self._process)
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        if self._worker is not None:
            with contextlib.suppress(BaseException):
                await self._worker
        while not self._bridge.requests.empty():
            self._bridge.requests.get_nowait().answer.cancel()
        if self._current is not None:
            self._current.answer.cancel()
        if self._runner is not None:
            await self._runner.cleanup()


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(process.wait(), STOP_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


class OvernightEnvGroupBuilder(EnvGroupBuilder):
    def __init__(self, config: EpisodeConfig, case: Case, group_size: int) -> None:
        self.config = config
        self.case = case
        self.group_size = group_size
        self._envs: list[OvernightEnv] = []

    def __getstate__(self) -> dict[str, Any]:
        return {"config": self.config, "case": self.case, "group_size": self.group_size}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(state["config"], state["case"], state["group_size"])

    async def make_envs(self) -> Sequence[Env]:
        # Called again by retrying strategies; every env made is stopped at cleanup.
        envs = [OvernightEnv(self.config, self.case) for _ in range(self.group_size)]
        self._envs += envs
        return envs

    async def cleanup(self) -> None:
        envs, self._envs = self._envs, []
        await asyncio.gather(*(env.close() for env in envs), return_exceptions=True)

    def logging_tags(self) -> list[str]:
        return [self.case.family]
