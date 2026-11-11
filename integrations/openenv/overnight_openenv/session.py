import asyncio
import contextlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from openenv.core.env_server.mcp_types import Tool
from openenv.core.harness import (
    Message,
    ResourceSession,
    ResourceSessionFactory,
    ToolResult,
    TrainingTrace,
    VerifyResult,
)
from openenv.core.harness.capture.contract import to_training_trace
from openenv.core.harness.capture.export import export_session
from openenv.core.harness.capture.server import create_app
from openenv.core.harness.capture.sessions import Session, Upstream, extract_api_key
from openenv.core.harness.capture.upstream import training_sampling

from overnight_openenv.cases import Case, family_cases, overnight_python

POLICY_KEY = "OVERNIGHT_POLICY_KEY"
STOP_SECONDS = 20.0
OPENROUTER_KEY = "OPENROUTER_API_KEY"
EPISODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class OvernightError(RuntimeError):
    """The episode says nothing about the policy: Overnight could not run or grade it, or the
    engine failed under it. TRL scores the rollout as unscorable and drops it."""


class InFlight:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.open: Counter[str] = Counter()
        self.abandoned: Counter[str] = Counter()
        self.lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            name.decode("latin-1"): value.decode("latin-1") for name, value in scope["headers"]
        }
        key = extract_api_key(headers) or ""
        answered = False
        disconnected = asyncio.Event()
        watcher: asyncio.Task[None] | None = None

        async def watch() -> None:
            # After the body, the server's next message is the client's disconnect.
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()

        async def receiving() -> Any:
            nonlocal watcher
            if watcher is not None:
                await disconnected.wait()
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                watcher = asyncio.ensure_future(watch())
            return message

        async def sending(message: Any) -> None:
            nonlocal answered
            if message["type"] == "http.response.body" and not message.get("more_body"):
                answered = not disconnected.is_set()
            await send(message)

        with self.lock:
            self.open[key] += 1
        try:
            await self.app(scope, receiving, sending)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
            with self.lock:
                self.open[key] -= 1
                if disconnected.is_set() and not answered:
                    self.abandoned[key] += 1

    def unanswered(self, key: str) -> int:
        with self.lock:
            return max(0, self.open[key]) + self.abandoned[key]


class CaptureProxy:
    def __init__(self, *, max_output_tokens: int | None) -> None:
        self.app = create_app(admin_key=secrets.token_hex(16), max_output_tokens=max_output_tokens)
        self.in_flight = InFlight(self.app)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.port: int = listener.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(self.in_flight, log_level="warning"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 30
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("OpenEnv's capture proxy did not start")
            time.sleep(0.05)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def open(self, upstream: Upstream, **options: Any) -> Session:
        client, level = asyncio.run(self.app.state.upstreams.resolve(upstream))
        upstream = replace(upstream, model=client.served_model or upstream.model)
        return self.app.state.registry.create(upstream=upstream, capture_level=level, **options)

    def close(self, session: Session) -> None:
        self.app.state.registry.delete(session.session_id)
        with self.in_flight.lock:
            self.in_flight.abandoned.pop(session.session_id, None)
            if self.in_flight.open[session.session_id] <= 0:
                del self.in_flight.open[session.session_id]

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def operator_key_variable(operator_model: dict[str, Any]) -> str:
    provider = operator_model.get("provider", "openrouter")
    if provider == "openrouter":
        return OPENROUTER_KEY
    if isinstance(provider, dict) and isinstance(provider.get("api_key_env"), str):
        return provider["api_key_env"]
    raise ValueError("operator_model.provider must be 'openrouter' or name its api_key_env")


class OvernightSession(ResourceSession):
    """One Overnight episode. `wait_for_completion` runs it; `fetch_training_trace` returns the
    policy calls the proxy captured; `verify` returns Overnight's reward.

    `wait_for_completion` returns 0 for a graded episode, and 1 when the agent under training
    failed it: its reward is 0, and a capture TRL cannot train on is dropped, never a contract
    error. An agent out of time with every policy call answered also scores 0: it raises
    `TimeoutError`, TRL's timed-out path, when its capture is trainable, and returns 1 otherwise.
    It raises `OvernightError`, which TRL scores as unscorable, when the episode says nothing
    about the policy: a policy call the engine had not answered when the episode ended, an engine
    fault on any policy call, an infrastructure failure, a refused request, an ungraded result,
    or a graded episode with no captured call or a capture TRL cannot train on."""

    def __init__(
        self,
        *,
        case: Case,
        proxy: CaptureProxy,
        capture: Session,
        request: dict[str, Any],
        environ: dict[str, str],
        command: list[str],
        timeout_s: float,
    ) -> None:
        self.case = case
        self._proxy = proxy
        self._capture = capture
        self._request = request
        self._environ = environ
        self._command = command
        self._timeout_s = timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.reward: float | None = None
        self.result: dict[str, Any] = {}

    def initial_messages(self) -> list[Message]:
        return [{"role": "user", "content": self.case.id}]

    def list_tools(self) -> list[Tool]:
        return []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(error="the agent owns its loop inside Overnight's episode")

    def verify(self, transcript: list[Message], final_state: Any | None = None) -> VerifyResult:
        return VerifyResult(
            env_reward=self.reward,
            done=self.reward is not None,
            metrics={"overnight_passed": float(self.result.get("verdict") == "passed")},
            artifacts={"overnight": self.result},
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None:
            _stop(process)
        self._proxy.close(self._capture)

    def wait_for_completion(self, timeout_s: float | None = None) -> int:
        result = self._run_worker(self._timeout_s if timeout_s is None else timeout_s)
        if self._capture.upstream_errors:
            raise OvernightError(
                f"{self._capture.upstream_errors} policy call(s) failed at the engine"
            )
        if result is None:
            self.result = {"episode": self._request["id"], "timed_out": True}
            self.reward = 0.0
            if self._trainable() is None:
                raise TimeoutError(f"the agent ran past {self._timeout_s} seconds")
            return 1
        if "error" in result:
            raise OvernightError(f"Overnight refused the episode: {result['error']}")
        failure = result["failure"]
        self.result = {
            "episode": result["id"],
            "scenario_id": result["scenario_id"],
            "verdict": result["verdict"],
            "score": result["score"],
            "failure": failure,
            "policy_model": result["policy_model"],
            "usage": result["usage"],
            "operator_usage": result["operator_usage"],
        }
        if failure is not None and failure["kind"] == "agent":
            self.reward = 0.0
            return 1
        if failure is not None:
            raise OvernightError(f"Overnight infrastructure failure: {failure}")
        if result["score"] is None:
            raise OvernightError(f"Overnight returned no score (verdict {result['verdict']})")
        if not self._capture.graph.stats()["n_turns"]:
            raise OvernightError("the episode was graded but no policy call reached the proxy")
        problem = self._trainable()
        if problem is not None:
            raise OvernightError(f"the capture cannot be trained on: {problem}")
        self.reward = float(result["score"])
        return 0

    def _trainable(self) -> str | None:
        try:
            self.fetch_training_trace()
        except (ValueError, TypeError, KeyError) as error:
            return str(error) or type(error).__name__
        return None

    def fetch_training_trace(self) -> TrainingTrace:
        document = export_session(
            self._capture, include_messages=True, capture_level=self._capture.capture_level
        )
        return to_training_trace(self._capture.graph, document)

    def _run_worker(self, timeout_s: float) -> dict[str, Any] | None:
        with self._lock:
            if self._closed:
                raise OvernightError("the session was closed before it ran")
            process = self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environ,
                start_new_session=True,
            )
        line = json.dumps(self._request).encode() + b"\n"
        try:
            stdout, stderr = process.communicate(line, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            unanswered = self._proxy.in_flight.unanswered(self._capture.session_id)
            _stop(process, drain=True)
            if unanswered:
                raise OvernightError(
                    f"the deadline passed with {unanswered} policy call(s) at the engine"
                ) from None
            return None
        except BaseException:
            _stop(process, drain=True)
            raise
        unanswered = self._proxy.in_flight.unanswered(self._capture.session_id)
        if unanswered:
            raise OvernightError(f"the episode ended with {unanswered} policy call(s) unanswered")
        lines = stdout.decode().strip().splitlines()
        if process.returncode != 0 or not lines:
            detail = stderr.decode().strip()[-2000:] or "<no output>"
            raise OvernightError(
                f"Overnight's rollout worker exited {process.returncode}: {detail}"
            )
        return json.loads(lines[-1])


def _stop(process: subprocess.Popen[bytes], drain: bool = False) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
    finish = process.communicate if drain else process.wait
    try:
        finish(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        finish()


class OvernightSessionFactory(ResourceSessionFactory[OvernightSession]):
    """Sessions over the cases of one generated family, for `HarnessAsyncGRPOTrainer`.

    Args:
        family: A folder written by `overnight generate`. Overnight's split check against
            `held_out` (default: the repository's `examples`) must pass before any case loads.
        llm_url: The engine TRL samples from and syncs weights into, for example vLLM with
            `--return-tokens-as-token-ids --logprobs-mode processed_logprobs`.
        model: The served model name. TRL sets it per session to the current LoRA adapter.
        agent: An Overnight agent document, or its path, that names the policy with
            `{policy.base_url}`, `{policy.api_key_env}` and `{policy.model}`.
        operator_model: Overnight's `OperatorModel` for persona cases: a fixed model that is not
            the policy. Its key is read from its variable here and sent on the worker's input.
        sampling: The full-vocabulary training policy; TRL passes it from its config.
        engine_api_key_env: The variable holding the engine's key, if it needs one. The key stays
            in the proxy.
        agent_timeout_s: The episode's deadline, setup and grading included. Past it the worker
            is interrupted; the episode scores 0 when every policy call was answered and is
            unscorable when one was still at the engine.
        max_model_calls: The proxy's per-episode call budget; 0 leaves it to the connector's cap.
        pass_env: Variables of the trainer's environment the worker keeps beside its own allow
            list (`overnight.adapters.rollout --clean-env`).
    """

    def __init__(
        self,
        family: str | Path,
        *,
        llm_url: str,
        model: str,
        agent: str | Path | dict[str, Any],
        operator_model: dict[str, Any],
        held_out: list[str | Path] | None = None,
        python: str | None = None,
        sampling: dict[str, Any] | None = None,
        engine_api_key_env: str | None = None,
        agent_timeout_s: float = 1800.0,
        max_model_calls: int = 0,
        max_output_tokens: int | None = 8192,
        pass_env: tuple[str, ...] = (),
        root: str | Path | None = None,
        keep: bool = False,
    ) -> None:
        if not operator_model or "model" not in operator_model:
            raise ValueError(
                "operator_model must name a fixed operator model: unset, a persona case would "
                "have no operator, and the policy under training must not answer as one"
            )
        self.operator_key_env = operator_key_variable(operator_model)
        if self.operator_key_env not in os.environ:
            raise ValueError(f"set {self.operator_key_env}, the operator model's key")
        if isinstance(agent, (str, Path)):
            agent = json.loads(Path(agent).read_text(encoding="utf-8"))
        text = json.dumps(agent)
        if "{policy.base_url}" not in text or "{policy.api_key_env}" not in text:
            raise ValueError(
                "the agent must name {policy.base_url} and {policy.api_key_env}, so its calls "
                "reach the capture proxy under its session key"
            )
        self.family = Path(family)
        self.held_out = [Path(folder) for folder in held_out or []]
        self.python = python or overnight_python()
        self.llm_url = llm_url
        self.model = model
        self.agent = agent
        self.operator_model = operator_model
        self.sampling = training_sampling(sampling) if sampling is not None else None
        self.engine_api_key_env = engine_api_key_env
        self.agent_timeout_s = agent_timeout_s
        self.max_model_calls = max_model_calls
        self.max_output_tokens = max_output_tokens
        self.pass_env = tuple(pass_env)
        self.root = Path(root) if root is not None else None
        self.keep = keep
        self._cases: dict[str, Case] | None = None
        self._proxy: CaptureProxy | None = None
        self._lock = threading.Lock()

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state.update(_proxy=None, _lock=None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state, _lock=threading.Lock())

    def cases(self) -> dict[str, Case]:
        with self._lock:
            if self._cases is None:
                self._cases = family_cases(self.python, self.family, self.held_out)
            return self._cases

    def prompt_rows(self) -> list[dict[str, Any]]:
        """Dataset rows for TRL: the case id as the prompt, which `create` reads back. The agent
        never sees it."""
        return [
            {"prompt": [{"role": "user", "content": case_id}], "case_id": case_id}
            for case_id in self.cases()
        ]

    def proxy(self) -> CaptureProxy:
        with self._lock:
            if self._proxy is None:
                self._proxy = CaptureProxy(max_output_tokens=self.max_output_tokens)
                if self.root is None:
                    self.root = Path(tempfile.mkdtemp(prefix="overnight-openenv-"))
            return self._proxy

    def close(self) -> None:
        with self._lock:
            proxy, self._proxy = self._proxy, None
        if proxy is not None:
            proxy.stop()

    def create(
        self, task: Any, seed: int | None = None, episode_id: str | None = None
    ) -> OvernightSession:
        case = self.cases()[_case_id(task)]
        proxy = self.proxy()
        engine_key = os.environ.get(self.engine_api_key_env) if self.engine_api_key_env else None
        capture = proxy.open(
            Upstream(llm_url=self.llm_url, model=self.model, api_key=engine_key),
            purpose="train",
            sampling=self.sampling,
            max_model_calls=self.max_model_calls,
            case=case.id,
        )
        episode = episode_id if episode_id and EPISODE_ID.fullmatch(episode_id) else uuid4().hex
        request: dict[str, Any] = {
            "id": episode,
            "scenario": case.scenario,
            "agent": self.agent,
            "policy": {
                "provider": {"base_url": proxy.base_url, "api_key_env": POLICY_KEY},
                "model": self.model,
            },
            "capture": False,
        }
        if case.has_persona:
            request["operator_model"] = self.operator_model
            # On the worker's input, never in its environment, which a host agent can read.
            request["operator_key"] = os.environ[self.operator_key_env]
        command = [self.python, "-m", "overnight.adapters.rollout", "--root", str(self.root)]
        for name in (POLICY_KEY, *self.pass_env):
            command += ["--pass-env", name]
        return OvernightSession(
            case=case,
            proxy=proxy,
            capture=capture,
            request=request,
            environ=os.environ | {POLICY_KEY: capture.session_id},
            command=[*command, "--clean-env", *(["--keep"] if self.keep else [])],
            timeout_s=self.agent_timeout_s,
        )


def _case_id(task: Any) -> str:
    if isinstance(task, list) and task and isinstance(task[-1], dict):
        return str(task[-1].get("content") or "")
    if isinstance(task, dict):
        return str(task.get("case_id") or task.get("content") or "")
    return str(task)
