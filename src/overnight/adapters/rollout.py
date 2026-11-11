"""A long-lived rollout worker for an outside trainer.

The trainer sends a case and its policy's model endpoint; the worker runs one episode through the
ordinary runner, with token capture on unless the request turns it off, and returns the score
with every captured model call. The worker knows no harness: the agent is a scenario agent document (a suite arm's `agent`), and the
policy endpoint reaches it only by replacing these tokens in the agent's strings:

    {policy.base_url}  {policy.api_key_env}  {policy.model}

so a connector receives the endpoint through its usual options, for example inline in
`overnight.adapters.acp --config-json`. Capture reaches the connector's provider relay through
the runner's `OVERNIGHT_CAPTURE_PATH` (see `run_capture`), and each process's calls are read back
from the capture file the runner assigned it, never from a file the agent named. A rollout's
`failure` tells an agent-caused failure from an infrastructure one. A persona operator answers with `operator_model` (a live model,
for example a fixed local one) or replays `operator_answers`; recorded answers fit only an agent
that follows the recorded path.

Run it as `python -m overnight.adapters.rollout --root DIR`: one JSON `RolloutRequest` per input
line, one JSON `Rollout` (or `{"id", "error"}`) per output line. The process keeps its caches,
such as the operation schemas, across episodes. A trainer that starts the worker from its own
environment adds `--clean-env` (and `--pass-env NAME` for each variable the episode needs), so
the worker's environment, and a host agent's, holds only `WORKER_ENVIRONMENT` and those names.
"""

import json
import os
import re
import shutil
import signal
import sys
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit
from uuid import uuid4

import click
from pydantic import (
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    ValidationError,
    model_validator,
)

from overnight.adapters.operator_model import ModelTransport
from overnight.adapters.provider import endpoint
from overnight.adapters.provider_relay import CapturedCall, UnansweredCall, read_capture
from overnight.domain.contracts import CompatibleProvider, Contract, JsonObject, OperatorModel
from overnight.domain.vocabulary import FailureKind
from overnight.execution.container import policy_url
from overnight.execution.evaluation import score
from overnight.execution.operator import (
    OperatorBudgetExceeded,
    OperatorTransport,
    RecordedTransport,
)
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import ResolvedScenario
from overnight.execution.usage import ProcessUsage
from overnight.execution.variations import VariationConfig, resolve_variant

type EpisodeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
# Tokens are replaced inside JSON strings such as `--config-json`, so no value may need escaping.
type Plain = Annotated[str, StringConstraints(pattern=r'^[^"\\\s{}]+$')]
TOKEN = re.compile(r"\{policy\.(base_url|api_key_env|model)\}")


class PolicyEndpoint(Contract):
    """The trained model: an OpenAI-compatible endpoint and the model name it serves."""

    provider: CompatibleProvider
    model: Plain

    @model_validator(mode="after")
    def plain_base_url(self) -> Self:
        if re.fullmatch(r'[^"\\\s{}]+', self.provider.base_url) is None:
            raise ValueError(
                "policy base_url may not contain quotes, backslashes, spaces or braces"
            )
        return self

    def tokens(self) -> dict[str, str]:
        return {
            "base_url": self.provider.base_url,
            "api_key_env": self.provider.api_key_env,
            "model": self.model,
        }


class RolloutRequest(Contract):
    """One episode: a case (scenario with optional overrides and variation), the agent that
    replaces the case's own, and who answers as the operator."""

    id: EpisodeId = Field(default_factory=lambda: uuid4().hex)
    scenario: JsonObject
    overrides: JsonObject | None = None
    variation: JsonObject | None = None
    agent: JsonObject | None = None
    policy: PolicyEndpoint | None = None
    operator_model: OperatorModel | None = None
    operator_answers: list[JsonObject] | None = None
    # The operator model's key, for a trainer that keeps it out of the worker's environment,
    # where other processes of its user could read it. It overrides `api_key_env`.
    operator_key: SecretStr | None = None
    # Off when the trainer records the policy's calls itself, for example at its own endpoint.
    capture: bool = True

    @model_validator(mode="after")
    def one_operator_source(self) -> Self:
        if self.operator_model is not None and self.operator_answers is not None:
            raise ValueError("pass operator_model or operator_answers, not both")
        if self.operator_key is not None and self.operator_model is None:
            raise ValueError("operator_key needs operator_model")
        return self


class CaptureFile(Contract):
    """The calls one agent process captured, in answer order. `source` names its capture file,
    relative to the episode's agent folder."""

    source: str
    calls: list[CapturedCall]


class RolloutFailure(Contract):
    """Why the episode did not finish as a graded run. `agent`: the agent under training caused
    it, for example its harness exited or answered out of protocol. `infrastructure`: anything
    else, including unusable captures; the episode says nothing about the policy."""

    kind: FailureKind
    type: str
    message: str


class Rollout(Contract):
    id: str
    scenario_id: str
    verdict: str
    score: float | None
    failure: RolloutFailure | None
    policy_model: str | None
    captures: list[CaptureFile]
    provenance: JsonObject
    usage: ProcessUsage
    operator_usage: ProcessUsage


def _substitute(value: JsonValue, tokens: dict[str, str]) -> tuple[JsonValue, bool]:
    if isinstance(value, str):
        # One pass, so a value is never read as another token.
        replaced, count = TOKEN.subn(lambda match: tokens[match.group(1)], value)
        return replaced, count > 0
    if isinstance(value, list):
        items = [_substitute(item, tokens) for item in value]
        return [item for item, _ in items], any(used for _, used in items)
    if isinstance(value, dict):
        entries = {key: _substitute(item, tokens) for key, item in value.items()}
        return {key: item for key, (item, _) in entries.items()}, any(
            used for _, used in entries.values()
        )
    return value, False


def _agent(request: RolloutRequest) -> JsonObject | None:
    """The request's agent with the policy endpoint filled in. A policy the agent never names
    would leave the agent on another model, so it is refused, as is an unfilled token."""
    if request.agent is None:
        if request.policy is not None:
            raise ValueError("a policy endpoint needs an agent that names it")
        return None
    if request.policy is None:
        if "{policy." in json.dumps(request.agent):
            raise ValueError("the agent names a policy endpoint; pass policy")
        return request.agent
    agent, used = _substitute(request.agent, request.policy.tokens())
    if not used:
        raise ValueError("the agent does not name the policy endpoint")
    assert isinstance(agent, dict)
    return agent


LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _route(request: RolloutRequest, contained: bool) -> tuple[RolloutRequest, int | None]:
    """A contained agent cannot reach the host's loopback, where a trainer such as verifiers
    serves the policy. For that agent only, the policy's one host port is forwarded through the
    agent's egress proxy, and the agent is given the proxy's address for it instead."""
    policy = request.policy
    if policy is None or not contained:
        return request, None
    parts = urlsplit(policy.provider.base_url)
    if parts.hostname not in LOOPBACK:
        return request, None
    if parts.port is None:
        raise ValueError("a loopback policy endpoint for a contained agent needs a port")
    provider = policy.provider.model_copy(update={"base_url": policy_url(parts.path)})
    routed = PolicyEndpoint(provider=provider, model=policy.model)
    return request.model_copy(update={"policy": routed}), parts.port


def _resolve(request: RolloutRequest) -> ResolvedScenario:
    return resolve_variant(
        _scenario(request),
        request.overrides,
        VariationConfig.model_validate(request.variation)
        if request.variation is not None
        else None,
    )


@contextmanager
def _withheld(request: RolloutRequest) -> Generator[None]:
    """Keep the operator model's key out of the episode's environment, which host agents
    inherit; the operator transport has already read it. A key the policy also uses stays."""
    operator = request.operator_model
    if operator is None or not isinstance(operator.provider, CompatibleProvider):
        yield
        return
    name = operator.provider.api_key_env
    if request.policy is not None and request.policy.provider.api_key_env == name:
        yield
        return
    value = os.environ.pop(name, None)
    try:
        yield
    finally:
        if value is not None:
            os.environ[name] = value


def _scenario(request: RolloutRequest) -> JsonObject:
    """The case with the request's agent and operator model, as a suite arm applies them."""
    scenario = dict(request.scenario)
    agent = _agent(request)
    if agent is not None:
        authored = scenario.get("agent")
        if "context" not in agent and isinstance(authored, dict) and "context" in authored:
            agent = agent | {"context": authored["context"]}
        scenario["agent"] = agent
    operator = scenario.get("operator")
    persona = operator.get("persona") if isinstance(operator, dict) else None
    if request.operator_model is not None and isinstance(persona, dict):
        assert isinstance(operator, dict)
        model = request.operator_model.model_dump(mode="json")
        scenario["operator"] = operator | {"persona": persona | {"model": model}}
    return scenario


def _operator(request: RolloutRequest, resolved: ResolvedScenario) -> OperatorTransport | None:
    persona = resolved.config.operator.persona
    if persona is None:
        if request.operator_answers is not None:
            raise ValueError("operator_answers need a scenario with an operator persona")
        return None
    if request.operator_answers is not None:
        return RecordedTransport.from_entries(request.operator_answers)
    if request.operator_key is not None:
        name = endpoint(persona.model.provider).api_key_env
        key = {name: request.operator_key.get_secret_value()}
        return ModelTransport(persona.model, resolved.config.seed, key)
    return ModelTransport(persona.model, resolved.config.seed)


# Failures the run records as infrastructure that the agent under training caused: it asked the
# operator past the run's operator budget. An eval keeps them invalid; a rollout scores them 0.
AGENT_CAUSED = frozenset({OperatorBudgetExceeded.__name__})


def _failure(verdict: JsonObject) -> RolloutFailure | None:
    """The run's own failure, classified as the suite classifies `agent_failed` and `invalid`,
    except that exhausting the operator budget is the agent's (`AGENT_CAUSED`)."""
    failure = verdict.get("failure")
    if not isinstance(failure, dict):
        return None
    kind = FailureKind(str(failure.get("kind")))
    if failure.get("type") in AGENT_CAUSED:
        kind = FailureKind.AGENT
    return RolloutFailure(
        kind=kind,
        type=str(failure.get("type")),
        message=str(failure.get("message")),
    )


def _unanswered(calls: list[UnansweredCall]) -> RolloutFailure:
    reasons = sorted({call.reason for call in calls})
    return RolloutFailure(
        kind=FailureKind.INFRASTRUCTURE,
        type="PolicyCallUnanswered",
        message=f"{len(calls)} policy call(s) not answered by the engine: {', '.join(reasons)}",
    )


# The variables a worker started with `--clean-env` keeps: what a process and Docker need to run.
# Anything else, such as a trainer's API keys, tokens or cloud credentials, stays out unless
# named with `--pass-env`.
WORKER_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TERM",
        "TZ",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    }
)
# Set in the environment a cleaned worker restarts with, so it restarts once.
CLEANED = "OVERNIGHT_ROLLOUT_CLEAN_ENV"


def worker_environment(environ: Mapping[str, str], passed: Iterable[str] = ()) -> dict[str, str]:
    """The variables of `environ` that a cleaned worker keeps: `WORKER_ENVIRONMENT` and `passed`."""
    names = WORKER_ENVIRONMENT | set(passed)
    return {name: value for name, value in environ.items() if name in names}


def _clean_environment(passed: Iterable[str]) -> None:
    """Restart this process with `worker_environment` only. Removing variables would not do:
    other processes of the same user, a host agent included, read the environment a process
    started with (`ps eww`), and a restart replaces it."""
    if os.environ.pop(CLEANED, None) is not None:
        return
    environ = worker_environment(os.environ, passed) | {CLEANED: "1"}
    os.execve(sys.executable, sys.orig_argv, environ)


class RolloutWorker:
    """Runs rollouts one at a time under `root`, one fresh directory per episode. The directory
    is removed after the rollout unless `keep` is set."""

    def __init__(self, root: Path, keep: bool = False) -> None:
        self.root = root
        self.keep = keep

    def rollout(self, request: RolloutRequest) -> Rollout:
        policy_model = request.policy.model if request.policy is not None else None
        resolved = _resolve(request)
        # Whether the agent is contained is read from the resolved case, after its overrides.
        container = getattr(resolved.config.agent, "container", None)
        request, policy_port = _route(request, container is not None)
        if policy_port is not None:
            resolved = _resolve(request)
        transport = _operator(request, resolved)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.root / request.id
        directory.mkdir(mode=0o700)
        try:
            agent = directory / "agent"
            with _withheld(request):
                result = run_scenario(
                    resolved,
                    agent_folder=agent,
                    operator_transport=transport,
                    capture=request.capture,
                    policy_port=policy_port,
                )
            failure = _failure(result.verdict)
            captures: list[CaptureFile] = []
            unanswered: list[UnansweredCall] = []
            try:
                # Only the files the runner assigned: anything else in the agent folder may be
                # the agent's own writing.
                for path in result.capture_paths:
                    capture = read_capture(path)
                    unanswered.extend(capture.unanswered)
                    if capture.calls:
                        captures.append(
                            CaptureFile(
                                source=path.relative_to(agent).as_posix(), calls=capture.calls
                            )
                        )
                if unanswered and (failure is None or failure.kind == FailureKind.AGENT):
                    # The engine, not the policy, ended these calls, whatever the harness did
                    # next: the episode says nothing about the policy.
                    failure = _unanswered(unanswered)
            except (OSError, ValueError) as error:
                captures = []
                failure = RolloutFailure(
                    kind=FailureKind.INFRASTRUCTURE,
                    type=type(error).__name__,
                    message=f"unreadable capture file: {error}",
                )
            if request.capture and not captures and result.usage.calls and failure is None:
                # A connector that does not pass `run_capture()` to its relay would give a score
                # with no tokens; the trainer must not mistake that for an episode without calls.
                failure = RolloutFailure(
                    kind=FailureKind.INFRASTRUCTURE,
                    type="CaptureMissing",
                    message="the agent made model calls but captured none",
                )
        finally:
            if not self.keep:
                shutil.rmtree(directory, ignore_errors=True)
        return Rollout(
            id=request.id,
            scenario_id=result.scenario_id,
            verdict=str(result.verdict["verdict"]),
            score=score(result.verdict, result.task_outcomes),
            failure=failure,
            policy_model=policy_model,
            captures=captures,
            provenance=result.provenance,
            usage=result.usage,
            operator_usage=result.operator_usage,
        )


@click.command()
@click.option("--root", type=click.Path(path_type=Path), required=True)
@click.option("--keep", is_flag=True, help="Keep each episode's agent folder under --root.")
@click.option(
    "--clean-env",
    is_flag=True,
    help="Restart with only the variables of WORKER_ENVIRONMENT and --pass-env.",
)
@click.option("--pass-env", multiple=True, help="A variable --clean-env keeps. Repeatable.")
def main(root: Path, keep: bool, clean_env: bool, pass_env: tuple[str, ...]) -> None:
    """Serve rollouts: one JSON request per input line, one JSON result per output line. An
    interrupt stops the worker after the runner removes the episode's processes and containers,
    even when the worker was started with interrupts ignored."""
    if clean_env:
        _clean_environment(pass_env)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    worker = RolloutWorker(root, keep)
    for line in sys.stdin:
        if not line.strip():
            continue
        identifier = None
        try:
            try:
                request = RolloutRequest.model_validate_json(line)
            except ValidationError as error:
                # Without the input, which may hold the operator key.
                problems = error.errors(include_url=False, include_input=False)
                raise ValueError(f"invalid rollout request: {problems}") from None
            identifier = request.id
            answer = worker.rollout(request).model_dump_json()
        except Exception as error:  # one failed episode must not stop the worker
            answer = json.dumps({"id": identifier, "error": f"{type(error).__name__}: {error}"})
        sys.stdout.write(answer + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
