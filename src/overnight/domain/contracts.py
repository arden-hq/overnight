from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import regex
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    TypeAdapter,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from overnight.domain.immutable import FrozenJson, FrozenObject
from overnight.domain.transport import MAX_EXCHANGE_TIMEOUT_SECONDS
from overnight.domain.values import JsonObject
from overnight.domain.vocabulary import (
    ActorRole,
    ApprovalDecision,
    NotificationKind,
    PolicyMode,
    PolicyOutcome,
    ResultStatus,
)

type Name = Annotated[str, Field(min_length=1)]
type Nonnegative = Annotated[int, Field(ge=0)]
type Path = list[str | int]
type Mode = Literal[PolicyMode.DENY, PolicyMode.ALLOW, PolicyMode.APPROVAL]


def _reject_explicit_null(value: object) -> object:
    if value is None:
        raise ValueError("value may be omitted but cannot be null")
    return value


type Omittable[T] = Annotated[T | SkipJsonSchema[None], BeforeValidator(_reject_explicit_null)]
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Names = list[Text]
NonemptyNames = Annotated[Names, Field(min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment:
        raise ValueError("base_url must be an http(s) URL without a query or fragment")
    return value.rstrip("/")


class CompatibleProvider(Contract):
    """An OpenAI-compatible model endpoint. `api_key_env` names the variable that holds the key;
    the key itself never enters configuration."""

    base_url: Annotated[str, AfterValidator(_base_url)]
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    reasoning_effort: str | None = Field(default=None, min_length=1)

    @property
    def host(self) -> str:
        host = urlsplit(self.base_url).hostname
        assert host is not None
        return host

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"


type Provider = Literal["openrouter"] | CompatibleProvider


class Fact(Contract):
    model_config = ConfigDict(frozen=True)


def _immutable_array(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


type FrozenPath = Annotated[tuple[str | int, ...], BeforeValidator(_immutable_array)]


class ReferenceTarget(Contract):
    result: Name
    path: Path


class Reference(Contract):
    target: ReferenceTarget = Field(alias="$ref")


class StateSource(Fact):
    source: Literal["state"]
    service_id: Name
    path: FrozenPath


class ResultSource(Fact):
    source: Literal["result"]
    result_id: Name
    path: FrozenPath


class StateEquals(StateSource):
    op: Literal["eq", "match"]
    value: FrozenJson


class ResultEquals(ResultSource):
    op: Literal["eq", "match"]
    value: FrozenJson


class StateExists(StateSource):
    op: Literal["exists"]


class ResultExists(ResultSource):
    op: Literal["exists"]


class CompoundCondition(Fact):
    op: Literal["all", "any"]
    conditions: Annotated[tuple[Condition, ...], BeforeValidator(_immutable_array)]


class NegatedCondition(Fact):
    op: Literal["not"]
    condition: Condition


type EqualityCondition = Annotated[StateEquals | ResultEquals, Field(discriminator="source")]
type ExistsCondition = Annotated[StateExists | ResultExists, Field(discriminator="source")]
type Condition = Annotated[
    EqualityCondition | ExistsCondition | CompoundCondition | NegatedCondition,
    Field(discriminator="op"),
]


class InvokeAction(Contract):
    kind: Literal["invoke", "request_approval"]
    id: Name
    service_id: Name
    operation: Name
    args: JsonObject = Field(default_factory=dict)
    task_id: Name | None = None
    chat_id: Name | None = None


class ReportAction(Contract):
    kind: Literal["report"]
    message: Name


class WaitAction(Contract):
    kind: Literal["wait"]
    until: Nonnegative | None = None


class DoneAction(Contract):
    kind: Literal["done"]


type Action = Annotated[
    InvokeAction | ReportAction | WaitAction | DoneAction, Field(discriminator="kind")
]
action_contract = TypeAdapter(Action)


class AgentConfig(Contract):
    context: JsonObject = Field(default_factory=dict)


class ScriptedAgent(AgentConfig):
    kind: Literal["scripted"]
    actions: list[Action] = Field(default_factory=list)


class ContainerMount(Contract):
    source: Name
    target: Name
    read_only: bool = True

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        if not (self.source.startswith("/") and self.target.startswith("/")):
            raise ValueError("container mount paths must be absolute")
        return self


class AgentContainer(Contract):
    """Run the agent command in a container that sees only its image and these mounts."""

    image: Name
    mounts: list[ContainerMount] = Field(default_factory=list)
    env: list[Name] = Field(default_factory=list)
    memory: Name = "4g"
    cpus: Annotated[float, Field(gt=0)] = 2
    pids: Annotated[int, Field(gt=0)] = 512
    allow_hosts: list[Name] = Field(default_factory=list)


class CommandAgent(AgentConfig):
    kind: Literal["command", "persistent_command"]
    command: Annotated[list[Name], Field(min_length=1)]
    container: AgentContainer | None = None
    timeout_seconds: Omittable[Annotated[float, Field(gt=0, le=MAX_EXCHANGE_TIMEOUT_SECONDS)]] = (
        Field(default_factory=lambda: None)
    )


class HarnessRequirements(Contract):
    simulated_clock: bool = True
    chat_isolation: bool = True
    cancel: bool = True
    steering: bool = False
    reset_scope: Literal["none", "process", "chat", "external"] = "none"


class HarnessAgent(AgentConfig):
    kind: Literal["harness"]
    command: Annotated[list[Name], Field(min_length=1)]
    required: HarnessRequirements = Field(default_factory=HarnessRequirements)
    timeout_seconds: float = Field(
        default=MAX_EXCHANGE_TIMEOUT_SECONDS, gt=0, le=MAX_EXCHANGE_TIMEOUT_SECONDS
    )
    container: AgentContainer | None = None
    run_on_host: bool = False

    @model_validator(mode="after")
    def validate_isolation(self) -> Self:
        if (self.container is None) != self.run_on_host:
            raise ValueError("a harness agent needs a container, or run_on_host: true")
        return self


type Agent = Annotated[ScriptedAgent | CommandAgent | HarnessAgent, Field(discriminator="kind")]


class PolicyRule(Contract):
    service_id: Name
    operation: Name
    mode: Mode


class PolicyConfig(Contract):
    default: Literal[PolicyMode.DENY, PolicyMode.ALLOW]
    rules: list[PolicyRule]


class OperatorMessage(Contract):
    """A message the operator sends at ``at`` (in ``chat_id`` when set)."""

    kind: Literal["message"]
    at: Nonnegative
    message: Name
    task_id: Name | None = None
    chat_id: Name | None = None


class OperatorStatus(Contract):
    kind: Literal["status"]
    at: Nonnegative


class OperatorPolicy(PolicyRule):
    kind: Literal["policy"]
    at: Nonnegative


class InvocationMatch(Contract):
    service_id: Name
    operation: Name
    args: JsonObject


class ScheduledDecision(Contract):
    """A scripted operator's timed decision on the oldest open request with exactly ``match``."""

    kind: Literal[ApprovalDecision.APPROVE, ApprovalDecision.DENY]
    at: Nonnegative
    match: InvocationMatch


type OperatorStep = Annotated[
    OperatorMessage | OperatorStatus | OperatorPolicy | ScheduledDecision,
    Field(discriminator="kind"),
]


class OperatorBrief(Contract):
    """What the user wants, knows and prefers for one task. Private: never in an observation."""

    task_id: Name | None = None
    wants: Text
    knows: list[Text] = Field(default_factory=list)
    prefers: list[Text] = Field(default_factory=list)


type Rate = Annotated[float, Field(ge=0, le=1000)]


class OperatorModel(Contract):
    """The model that plays the operator persona, and the bounds on its use in one run. The
    provider key is read from the provider's environment variable, never from the scenario."""

    provider: Provider = "openrouter"
    model: Name = "openai/gpt-6-luna"
    input_usd_per_million: Rate = 0.2
    output_usd_per_million: Rate = 0.75
    max_calls: Annotated[int, Field(ge=1, le=200)] = 30
    max_cost_usd: Annotated[float, Field(gt=0, le=10)] = 0.5
    max_output_tokens: Annotated[int, Field(ge=64, le=2048)] = 400
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 60


class OperatorPersona(Contract):
    description: Text
    briefs: Annotated[list[OperatorBrief], Field(min_length=1)]
    model: OperatorModel = Field(default_factory=OperatorModel)


class OperatorConfig(Contract):
    policy: PolicyConfig
    steps: list[OperatorStep]
    persona: OperatorPersona | None = None


class OperatorRequestBase(Contract):
    id: Name
    time: Nonnegative
    task_id: Name | None = None
    chat_id: Name | None = None


class ReportRequest(OperatorRequestBase):
    """An agent's report or question to the operator, verbatim."""

    kind: Literal["report"]
    message: Name


class ApprovalRequest(OperatorRequestBase):
    """A gated call that waits for the operator's decision."""

    kind: Literal["approval"]
    invocation_id: Name
    service_id: Name
    operation: Name
    args: JsonObject
    policy_revision: Nonnegative


type OperatorRequest = Annotated[ReportRequest | ApprovalRequest, Field(discriminator="kind")]


class Reply(Contract):
    """The operator's answer to a report. ``message`` None: the report needs no answer."""

    kind: Literal["reply"]
    request_id: Name
    message: Text | None = None


class Decision(Contract):
    """The operator's answer to an approval request; ``message`` goes to the asking chat."""

    kind: Literal["decision"]
    request_id: Name
    decision: Literal[ApprovalDecision.APPROVE, ApprovalDecision.DENY]
    message: Text | None = None


type OperatorAnswer = Annotated[Reply | Decision, Field(discriminator="kind")]


class CheckLabel(Contract):
    label: Text | None = None


class EqualsCheck(CheckLabel):
    source: Literal["state", "trace", "agent_report"] = "state"
    op: Literal["eq", "match"]
    path: Path
    value: JsonValue


class PresenceCheck(CheckLabel):
    source: Literal["state", "trace", "agent_report"] = "state"
    op: Literal["exists", "unchanged"]
    path: Path

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.op == "unchanged" and self.source != "state":
            raise ValueError("unchanged checks require state evidence")
        return self


type Check = Annotated[EqualsCheck | PresenceCheck, Field(discriminator="op")]


class ForbiddenCheck(CheckLabel):
    source: Literal["trace"] = "trace"
    match: Annotated[JsonObject, Field(min_length=1)]


class ClaimCheck(CheckLabel):
    """If an agent report matches ``pattern``, the world must back it up with ``requires``."""

    pattern: Name
    requires: Check

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        try:
            regex.compile(self.pattern)
        except regex.error as error:
            raise ValueError(f"invalid claim pattern: {error}") from error
        if self.requires.source == "agent_report":
            raise ValueError("a claim must be checked against state or trace evidence")
        return self


class Checks(Contract):
    outcomes: list[Check] = Field(default_factory=list)
    forbidden: list[ForbiddenCheck] = Field(default_factory=list)
    collateral: list[Check] = Field(default_factory=list)
    claims: list[ClaimCheck] = Field(default_factory=list)


class EpisodeTask(Contract):
    id: Name
    chat_id: Name
    message: Name
    start_at: Nonnegative | None = None
    deadline: Nonnegative | None = None
    memory: Literal["retain", "reset"] = "retain"
    checks: Checks = Field(default_factory=Checks)

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if (
            self.start_at is not None
            and self.deadline is not None
            and self.deadline < self.start_at
        ):
            raise ValueError("task deadline precedes start_at")
        return self


type ActorGrants = Annotated[
    Mapping[str, Annotated[tuple[Name, ...], BeforeValidator(_immutable_array)]],
    AfterValidator(MappingProxyType),
    PlainSerializer(lambda grants: {key: list(value) for key, value in grants.items()}),
]


class Actor(Fact):
    id: Name
    role: Literal[ActorRole.OPERATOR, ActorRole.AGENT, ActorRole.PERSON, ActorRole.SYSTEM]
    read_services: Annotated[tuple[Name, ...], BeforeValidator(_immutable_array)] = ()
    operations: ActorGrants = Field(default_factory=dict, validate_default=True)
    persona: FrozenObject = Field(default_factory=dict, validate_default=True)


class ServiceConfig(Contract):
    id: Name
    kind: Text
    state: JsonObject


class Invocation(Fact):
    id: Name
    actor_id: Name
    service_id: Name
    operation: Name
    args: FrozenObject = Field(default_factory=dict, validate_default=True)
    task_id: Name | None = None
    chat_id: Name | None = None
    cause_id: Name | None = None


class Notification(Fact):
    """A world fact addressed to one agent: another actor's effect, a due reminder, or an operator
    message. Its id derives from its cause and recipient, which stay in the trace."""

    id: Name
    time: Nonnegative
    kind: Annotated[NotificationKind, Field(strict=False)]
    service_id: Name | None
    ref: Name | None = None
    summary: Text
    chat_id: Name | None = None


class Event(Fact):
    id: Name
    invocation: Invocation
    at: Nonnegative | None = None
    dependencies: Annotated[tuple[Name, ...], BeforeValidator(_immutable_array)] = ()
    delay: Nonnegative = 0
    condition: Condition | None = None


class FaultSpec(Fact):
    id: Name
    service_id: Name
    operation: Name
    remaining: Nonnegative = 1
    probability: Annotated[float, Field(ge=0, le=1)] = 1.0
    start: Nonnegative | None = None
    end: Nonnegative | None = None
    error: str = "fault"

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("fault end precedes start")
        return self


class Scenario(Contract):
    id: Name
    seed: Nonnegative = 0
    start_time: Nonnegative
    horizon: Nonnegative
    max_steps: Annotated[int, Field(gt=0)]
    tasks: list[EpisodeTask] = Field(default_factory=list)
    actors: Annotated[list[Actor], Field(min_length=1)]
    services: Annotated[list[ServiceConfig], Field(min_length=1)]
    events: list[Event] = Field(default_factory=list)
    faults: list[FaultSpec] = Field(default_factory=list)
    agent: Agent
    operator: OperatorConfig
    checks: Checks

    @model_validator(mode="after")
    def validate_horizon(self) -> Self:
        if self.horizon < self.start_time:
            raise ValueError("horizon precedes start_time")
        return self


class Overrides(Contract):
    data: dict[str, JsonObject] = Field(default_factory=dict)
    people: dict[str, JsonObject] = Field(default_factory=dict)
    faults: Omittable[list[FaultSpec]] = Field(default_factory=lambda: None)


class PolicyReceipt(Contract):
    id: Name
    invocation_id: Name
    status: Literal[PolicyOutcome.ALLOWED, PolicyOutcome.PENDING, PolicyOutcome.APPROVED]
    time: int
    policy_revision: int | None = None


class ResultReceipt(Contract):
    id: Name
    invocation_id: Name
    status: Literal[ResultStatus.SUCCEEDED, ResultStatus.FAILED]
    value: JsonValue
    error: str | None
    time: int
    event_id: str | None = None


class DeniedReceipt(Contract):
    id: Name
    invocation_id: Name
    status: Literal[ResultStatus.DENIED]
    time: int
    policy_revision: int | None = None
    value: JsonValue = None
    error: str | None = None
    event_id: str | None = None


class WaitReceipt(Contract):
    """A deferred ``system.wait_for_notification``: its result receipt follows under the same id."""

    id: Name
    invocation_id: Name
    status: Literal[ResultStatus.WAITING]
    time: int
    until: Nonnegative


type Receipt = Annotated[
    PolicyReceipt | ResultReceipt | DeniedReceipt | WaitReceipt, Field(discriminator="status")
]
receipt_contract = TypeAdapter(Receipt)


class ScenarioError(ValueError): ...
