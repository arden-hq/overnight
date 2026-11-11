import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import TypeAdapter

from overnight.domain.actors import execution_actors
from overnight.domain.contracts import (
    Action,
    ApprovalRequest,
    CommandAgent,
    Decision,
    DoneAction,
    HarnessAgent,
    Invocation,
    InvokeAction,
    JsonObject,
    OperatorAnswer,
    OperatorMessage,
    OperatorPolicy,
    OperatorRequest,
    OperatorStatus,
    OperatorStep,
    Receipt,
    Reply,
    ReportAction,
    ReportRequest,
    ScenarioError,
    ScheduledDecision,
    WaitAction,
    receipt_contract,
)
from overnight.domain.models import PendingWait
from overnight.domain.values import json_object, json_value
from overnight.domain.vocabulary import (
    ApprovalDecision,
    FailureKind,
    PolicyOutcome,
    ResultStatus,
    RunStatus,
)
from overnight.execution.adapters import (
    ActionRejected,
    AdapterError,
    AgentAdapter,
    AgentBehaviorError,
    CommandAdapter,
    PersistentCommandAdapter,
    describe_failure,
    make_adapter,
    next_action,
)
from overnight.execution.evaluation import evaluate
from overnight.execution.harness import (
    HarnessAdapter,
    delivered_ids,
    harness_busy,
    harness_pending,
    harness_wake,
    prepare_harness,
)
from overnight.execution.operator import (
    AnsweredRequest,
    OperatorError,
    OperatorTransport,
    OperatorView,
    TranscriptLine,
    make_operator,
)
from overnight.execution.policy import ExecutionPolicy
from overnight.execution.provenance import (
    backend_provenance,
    read_connector_sidecars,
    run_provenance,
)
from overnight.execution.scenario import ResolvedScenario, construct_world
from overnight.execution.usage import NO_CALLS, UNMEASURED, ProcessUsage, read_usage
from overnight.world.engine import InvocationRejected


@dataclass
class RunResult:
    scenario_id: str
    final_state: JsonObject
    trace: list[JsonObject]
    verdict: JsonObject
    status: RunStatus
    steps: int
    provenance: JsonObject
    usage: ProcessUsage = UNMEASURED
    task_outcomes: list[JsonObject] | None = None
    operator_usage: ProcessUsage = NO_CALLS
    capture_paths: list[Path] = field(default_factory=list)
    """The capture files this run assigned its agent processes, in start order (training only)."""

    def to_dict(self) -> JsonObject:
        value = asdict(self)
        del value["capture_paths"]
        if self.task_outcomes is None:
            del value["task_outcomes"]
        return value


class OperatorSchedule:
    """The operator's timed message, status and policy steps in (at, source index) order. Both
    operators share them; timed decisions belong to the scripted operator."""

    def __init__(self, steps: list[OperatorStep]) -> None:
        self.steps = sorted(
            (
                (index, step)
                for index, step in enumerate(steps)
                if not isinstance(step, ScheduledDecision)
            ),
            key=lambda indexed_step: (indexed_step[1].at, indexed_step[0]),
        )
        self.completed: set[int] = set()

    def _open(self) -> Iterator[tuple[int, OperatorStep]]:
        for index, step in self.steps:
            if index not in self.completed:
                yield index, step

    def due(self, now: int) -> Iterator[tuple[int, OperatorStep]]:
        for index, step in self._open():
            if step.at > now:
                continue
            self.completed.add(index)
            yield index, step

    def next_time(self, now: int) -> int | None:
        """The next time any step is due."""
        return min((step.at for _, step in self._open() if step.at > now), default=None)

    def next_work_time(self, now: int, chat_id: str | None) -> int | None:
        """The next step that is still work for a chat: its messages, status and policy steps."""
        return min(
            (
                step.at
                for _, step in self._open()
                if step.at > now
                and not (isinstance(step, OperatorMessage) and not _in_chat(step, chat_id))
            ),
            default=None,
        )

    def pending_kinds(self) -> list[str]:
        return [step.kind for _, step in self._open()]


def _in_chat(step: OperatorMessage, chat_id: str | None) -> bool:
    return chat_id is None or step.chat_id in {None, chat_id}


request_contract = TypeAdapter(OperatorRequest)
answer_contract = TypeAdapter(OperatorAnswer)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


type LaneStatus = Literal["pending", "active", "idle", "done", "deadline"]
FINISHED: frozenset[str] = frozenset({"done", "deadline"})


@dataclass(frozen=True)
class Block:
    """Why an active lane is not observed: until a time, until a receipt settles, or both (either
    wakes it). With neither, it waits for the end of the run. ``offered`` holds the notifications
    the lane had already been shown when it blocked; only a newer one wakes a harness lane early."""

    until: int | None = None
    receipt_id: str | None = None
    offered: frozenset[str] = frozenset()


@dataclass(kw_only=True)
class Lane:
    """One conversation with the agent. A harness lane is idle after its turn ends and is observed
    again only when a notification for it exists or its native wake comes."""

    chat_id: str | None
    status: LaneStatus = "active"
    block: Block | None = None
    steps: int = 0

    @property
    def task_id(self) -> str | None:
        return None

    @property
    def start_at(self) -> int | None:
        return None

    @property
    def deadline(self) -> int | None:
        return None


class Trial:
    def __init__(
        self,
        scenario: ResolvedScenario,
        agent: AgentAdapter,
        operator_transport: OperatorTransport | None = None,
    ) -> None:
        self.scenario = scenario
        self.backend_provenance = backend_provenance()
        self.config = scenario.config
        self.actors = execution_actors(self.config.actors)
        self.world = construct_world(self.config, service_definitions=scenario.service_definitions)
        self.initial_snapshot = self.world.snapshot_for_evaluator()
        self.policy = ExecutionPolicy(self.config.operator.policy)
        self.schedule = OperatorSchedule(self.config.operator.steps)
        self.operator = make_operator(self.config, operator_transport, scenario.service_definitions)
        self.requests: dict[str, OperatorRequest] = {}
        self.open_approvals: dict[str, ApprovalRequest] = {}
        self.consulted: set[tuple[str, int]] = set()
        self.agent = agent
        self.messages: list[JsonObject] = []
        self.receipts: list[Receipt] = []
        self.reports: list[JsonObject] = []
        self.status = RunStatus.RUNNING
        self.action_count = 0
        self.failure: JsonObject | None = None
        self.rejected_actions = 0
        self.last_cause_id: str | None = None
        self.waiting: list[str] = []
        self.turn_cursor = 0
        self.lanes: Sequence[Lane] = self.initial_lanes()
        for lane in self.lanes:
            if lane.status == "active":
                self.world.open_inbox(self.actors.agent, lane.chat_id)

    def initial_lanes(self) -> Sequence[Lane]:
        return [Lane(chat_id=None)]

    def adapter_for(self, lane: Lane) -> AgentAdapter:
        return self.agent

    def lane_receipts(self, lane: Lane) -> Sequence[Receipt]:
        return self.receipts

    def receipt_json(self) -> list[JsonObject]:
        return [receipt.model_dump(mode="json", exclude_unset=True) for receipt in self.receipts]

    def add_receipt(self, receipt: JsonObject) -> None:
        self.receipts.append(receipt_contract.validate_python(receipt))

    def notifications(self, lane: Lane) -> list[JsonObject]:
        return [
            notification.model_dump(mode="json")
            for notification in self.world.undelivered(self.actors.agent, lane.chat_id)
        ]

    def apply_due_operator_steps(self) -> None:
        for index, step in self.schedule.due(self.world.now):
            self.apply_due_decisions((step.at, index))
            match step:
                case OperatorMessage():
                    self.deliver_operator_message(step, f"step-{index}")
                case OperatorStatus():
                    self.record_operator_status()
                case OperatorPolicy():
                    self.change_operator_policy(step)
        self.apply_due_decisions(None)

    def apply_due_decisions(self, before: tuple[int, int] | None) -> None:
        for answered in self.operator.due(self.world.now, self.open_requests(), before):
            self.apply_answer(answered)

    def open_requests(self) -> list[ApprovalRequest]:
        """Approval requests still awaiting a decision, oldest first."""
        return [
            request
            for invocation_id, request in self.open_approvals.items()
            if self.policy.awaiting_decision(invocation_id)
        ]

    def deliver_operator_message(
        self, step: OperatorMessage, message_id: str, request_id: str | None = None
    ) -> None:
        self.record_operator_message(step, message_id, request_id)
        self.world.post_operator_message(self.actors.agent, step.chat_id, message_id, step.message)

    def record_operator_message(
        self, step: OperatorMessage, message_id: str, request_id: str | None = None
    ) -> None:
        message: JsonObject = {
            "id": message_id,
            "time": self.world.now,
            "message": step.message,
            "task_id": step.task_id,
            "chat_id": step.chat_id,
        }
        self.messages.append(message)
        self.world.record(
            "operator_message",
            message if request_id is None else {**message, "request_id": request_id},
            actor_id=self.actors.operator,
            task_id=step.task_id,
            chat_id=step.chat_id,
        )

    def transcript_chat(self, request: OperatorRequest) -> str | None:
        """The chat whose conversation the operator is shown; None shows every entry."""
        return None

    def transcript(self, request: OperatorRequest, before: int) -> list[TranscriptLine]:
        """What the user saw in the asking chat before this request: their messages, the agent's
        reports, earlier approval requests and their decisions. No tools, state or checks."""
        chat_id = self.transcript_chat(request)
        lines: list[TranscriptLine] = []
        for entry in self.world.trace[:before]:
            if chat_id is not None and entry.chat_id != chat_id:
                continue
            payload = json_object(entry.payload)
            match entry.kind:
                case "operator_message" | "agent_report":
                    speaker = "you" if entry.kind == "operator_message" else "assistant"
                    lines.append(TranscriptLine(entry.time, speaker, str(payload["message"])))
                case "operator_request":
                    asked = request_contract.validate_python(payload)
                    if isinstance(asked, ApprovalRequest):
                        call = f"{asked.service_id}.{asked.operation} with {_canonical(asked.args)}"
                        lines.append(TranscriptLine(entry.time, "approval request", call))
                case "operator_answer":
                    answer = answer_contract.validate_python(payload["answer"])
                    if isinstance(answer, Decision):
                        verdict = "approved" if answer.decision == "approve" else "denied"
                        lines.append(TranscriptLine(entry.time, "your decision", verdict))
        return lines

    def consult(self, request: OperatorRequest, before: int) -> None:
        """Hand one request to the operator at the current world time and apply its answer. An
        approval request stays open until answered; a report request enters the trace only when
        the operator answers it or fails to, since an operator may leave reports unanswered."""
        view = OperatorView(request.task_id, self.transcript(request, before))
        invocation_id = request.invocation_id if isinstance(request, ApprovalRequest) else None
        if isinstance(request, ApprovalRequest):
            self.record_request(request)
            self.open_approvals[request.invocation_id] = request
        try:
            answered = self.operator.answer(request, view)
        except OperatorError as error:
            if isinstance(request, ReportRequest):
                self.record_request(request)
            self.world.record(
                "operator_failure",
                {
                    "request_id": request.id,
                    "error": str(error),
                    "calls": [call.model_dump(mode="json") for call in error.calls],
                },
                actor_id=self.actors.operator,
                invocation_id=invocation_id,
                task_id=request.task_id,
                chat_id=request.chat_id,
            )
            raise
        if answered is None:
            return
        if isinstance(request, ReportRequest):
            self.record_request(request)
        self.apply_answer(answered)

    def record_request(self, request: OperatorRequest) -> None:
        self.requests[request.id] = request
        self.world.record(
            "operator_request",
            request.model_dump(mode="json"),
            actor_id=self.actors.operator,
            invocation_id=request.invocation_id if isinstance(request, ApprovalRequest) else None,
            task_id=request.task_id,
            chat_id=request.chat_id,
        )

    def next_request_id(self) -> str:
        return f"op-{len(self.requests) + 1}"

    def apply_answer(self, answered: AnsweredRequest) -> None:
        """Apply an answer to the exact request it names, then send its message to that chat."""
        answer = answered.answer
        request = self.requests[answer.request_id]
        self.world.record(
            "operator_answer",
            {
                "answer": answer.model_dump(mode="json"),
                "evidence": answered.evidence.model_dump(mode="json"),
            },
            actor_id=self.actors.operator,
            invocation_id=request.invocation_id if isinstance(request, ApprovalRequest) else None,
            task_id=request.task_id,
            chat_id=request.chat_id,
        )
        if isinstance(answer, Decision) and isinstance(request, ApprovalRequest):
            self.open_approvals.pop(request.invocation_id, None)
            self.apply_operator_decision(request, answer)
        elif not (isinstance(answer, Reply) and isinstance(request, ReportRequest)):
            raise ScenarioError(f"operator answer {answer.kind} does not fit {request.kind}")
        if answer.message is not None:
            self.deliver_operator_message(
                OperatorMessage(
                    kind="message",
                    at=self.world.now,
                    message=answer.message,
                    task_id=request.task_id,
                    chat_id=request.chat_id,
                ),
                f"answer-{request.id}",
                request.id,
            )

    def record_operator_status(self) -> None:
        self.world.record(
            "operator_status",
            {
                "time": self.world.now,
                "agent_status": self.status,
                "reports": self.reports,
                "receipts": self.receipt_json(),
                "policy_revision": self.policy.revision,
            },
            actor_id=self.actors.operator,
        )

    def change_operator_policy(self, step: OperatorPolicy) -> None:
        self.policy.change(step.service_id, step.operation, step.mode)
        self.world.record(
            "policy_change",
            {
                "service_id": step.service_id,
                "operation": step.operation,
                "mode": step.mode,
                "revision": self.policy.revision,
            },
            actor_id=self.actors.operator,
        )

    def apply_operator_decision(self, request: ApprovalRequest, answer: Decision) -> None:
        invocation_id = request.invocation_id
        approved = answer.decision == ApprovalDecision.APPROVE
        decision = self.policy.decide(invocation_id, approved)
        self.add_receipt(
            {
                "id": invocation_id,
                "invocation_id": invocation_id,
                "status": PolicyOutcome.APPROVED if approved else PolicyOutcome.DENIED,
                "time": self.world.now,
                "policy_revision": decision.revision,
            }
        )
        self.world.record(
            "operator_approval",
            {
                "invocation_id": invocation_id,
                "decision": answer.decision,
                "revision": decision.revision,
                "fingerprint": decision.fingerprint,
                "request_id": request.id,
            },
            actor_id=self.actors.operator,
            invocation_id=invocation_id,
            task_id=decision.invocation.task_id,
            chat_id=decision.invocation.chat_id,
        )

    def invoke(self, action: InvokeAction) -> None:
        invocation = Invocation.model_validate(
            {
                "id": action.id,
                "actor_id": self.actors.agent,
                "service_id": action.service_id,
                "operation": action.operation,
                "args": action.args,
                "task_id": action.task_id,
                "chat_id": action.chat_id,
                "cause_id": self.last_cause_id,
            }
        )
        decision = self.policy.request(invocation)
        self.world.record(
            "policy_check",
            {
                "decision": decision,
                "revision": self.policy.revision,
                "service_id": invocation.service_id,
                "operation": invocation.operation,
                "args": json_value(
                    self.world.canonical_args(
                        invocation.service_id, invocation.operation, json_object(invocation.args)
                    )
                ),
            },
            actor_id=self.actors.agent,
            invocation_id=invocation.id,
            task_id=invocation.task_id,
            chat_id=invocation.chat_id,
            cause_id=invocation.cause_id,
        )
        if action.kind == "invoke" and decision == PolicyOutcome.ALLOWED:
            try:
                result = self.world.invoke(invocation)
            except InvocationRejected as error:
                raise ActionRejected(f"invalid invocation: {error}", action.id) from error
            self.policy.consume(invocation)
            if isinstance(result, PendingWait):
                self.add_receipt(
                    {
                        "id": invocation.id,
                        "invocation_id": invocation.id,
                        "status": ResultStatus.WAITING,
                        "time": self.world.now,
                        "until": result.until,
                    }
                )
                self.waiting.append(invocation.id)
            else:
                self.add_receipt(result.model_dump(mode="json"))
            self.last_cause_id = invocation.id
        else:
            self.add_receipt(
                {
                    "id": invocation.id,
                    "invocation_id": invocation.id,
                    "status": decision,
                    "time": self.world.now,
                }
            )
            asked = (invocation.id, self.policy.revision)
            if decision == PolicyOutcome.PENDING and asked not in self.consulted:
                self.consulted.add(asked)
                self.consult(
                    ApprovalRequest(
                        kind="approval",
                        id=self.next_request_id(),
                        time=self.world.now,
                        task_id=invocation.task_id,
                        chat_id=invocation.chat_id,
                        invocation_id=invocation.id,
                        service_id=invocation.service_id,
                        operation=invocation.operation,
                        args=json_object(invocation.args),
                        policy_revision=self.policy.revision,
                    ),
                    len(self.world.trace),
                )

    def collect_waits(self) -> None:
        """Add the result receipt of every wait the world has settled since the last look."""
        for invocation_id in list(self.waiting):
            result = self.world.results.get(invocation_id)
            if result is not None:
                self.add_receipt(result.model_dump(mode="json"))
                self.waiting.remove(invocation_id)

    def invoke_lane(self, lane: Lane, action: InvokeAction) -> None:
        self.invoke(action)

    def record_report(self, lane: Lane, message: str) -> None:
        report: JsonObject = {"time": self.world.now, "message": message}
        self.reports.append(report)
        self.world.record("agent_report", report, actor_id=self.actors.agent)

    def latest_receipt(self, lane: Lane) -> Receipt | None:
        receipts = self.lane_receipts(lane)
        return receipts[-1] if receipts else None

    def unsettled(self, receipt: Receipt) -> bool:
        """Whether a receipt still awaits its outcome: a wait the world has not resolved, or an
        approval request the operator can still decide. A policy change cancels the requests it
        clears, so their ``pending`` receipts await nothing."""
        if receipt.status == ResultStatus.WAITING:
            return True
        return receipt.status == PolicyOutcome.PENDING and self.policy.awaiting_decision(
            receipt.invocation_id
        )

    def receipt_settled(self, lane: Lane, receipt_id: str) -> bool:
        latest = next(
            (receipt for receipt in reversed(self.lane_receipts(lane)) if receipt.id == receipt_id),
            None,
        )
        return latest is not None and not self.unsettled(latest)

    def wait(self, lane: Lane, action: WaitAction) -> None:
        if action.until is not None and not self.world.now < action.until <= self.config.horizon:
            raise ActionRejected("wait.until must be a future time within horizon")
        adapter = self.adapter_for(lane)
        if harness_busy(adapter):
            return
        harness_wake(adapter, self.world.now)
        latest = self.latest_receipt(lane)
        receipt_id = latest.id if latest is not None and self.unsettled(latest) else None
        until = action.until
        if until is None and receipt_id is None:
            target = self.next_time()
            until = target if target > self.world.now else None
        offered = frozenset(str(note["id"]) for note in self.notifications(lane))
        lane.block = Block(until=until, receipt_id=receipt_id, offered=offered)
        self.world.record(
            "agent_wait",
            {"until": until, "receipt_id": receipt_id},
            actor_id=self.actors.agent,
            task_id=lane.task_id,
            chat_id=lane.chat_id,
        )

    def finish_lane(self, lane: Lane, status: LaneStatus) -> None:
        """End a lane. A lane that held its chat (active or idle) leaves nothing behind in it: the
        harness's native work is cancelled and the lane's open waits time out now."""
        if lane.status in {"active", "idle"}:
            self.retire_chat(lane)
        lane.status = status
        lane.block = None
        if status == "done":
            self.world.record(
                "agent_done",
                {},
                actor_id=self.actors.agent,
                task_id=lane.task_id,
                chat_id=lane.chat_id,
            )

    def retire_chat(self, lane: Lane) -> None:
        adapter = self.adapter_for(lane)
        if isinstance(adapter, HarnessAdapter) and (
            harness_pending(adapter)
            or harness_busy(adapter)
            or adapter.status.next_wake is not None
        ):
            adapter.cancel()
        self.expire_waits(lane)

    def expire_waits(self, lane: Lane) -> None:
        """Time out the lane's open waits now and record their results, so no wait outlives the
        turn that made it and takes a notification meant for a later turn."""
        self.world.expire(self.actors.agent, lane.chat_id)
        self.collect_waits()

    def done(self, lane: Lane) -> None:
        adapter = self.adapter_for(lane)
        if harness_pending(adapter):
            raise AgentBehaviorError("agent cannot finish with pending work")
        if not isinstance(adapter, HarnessAdapter):
            self.finish_lane(lane, "done")
            return
        harness_wake(adapter, self.world.now)
        self.expire_waits(lane)
        lane.status = "idle"
        lane.block = None
        self.world.record(
            "agent_idle", {}, actor_id=self.actors.agent, task_id=lane.task_id, chat_id=lane.chat_id
        )

    def act(self, action: Action, lane: Lane | None = None) -> None:
        lane = self.lanes[0] if lane is None else lane
        match action:
            case InvokeAction():
                self.invoke_lane(lane, action)
            case ReportAction():
                before = len(self.world.trace)
                # Surrounding whitespace of a report never decides a check, so it is stored trimmed.
                self.record_report(lane, action.message.strip())
                self.consult(
                    ReportRequest(
                        kind="report",
                        id=self.next_request_id(),
                        time=self.world.now,
                        task_id=lane.task_id,
                        chat_id=lane.chat_id,
                        message=action.message,
                    ),
                    before,
                )
            case WaitAction():
                self.wait(lane, action)
            case DoneAction():
                self.done(lane)

    def observe_agent(self) -> JsonObject:
        observation = json_object(
            {
                "time": self.world.now,
                "horizon": self.config.horizon,
                "context": self.config.agent.context,
                "world": self.world.observe(self.actors.agent),
                "operations": self.world.operations_for(self.actors.agent),
                "messages": self.messages,
                "notifications": self.notifications(self.lanes[0]),
                "receipts": self.receipt_json(),
                "policy_revision": self.policy.revision,
            }
        )
        self.world.record("agent_observation", observation, actor_id=self.actors.agent)
        return observation

    def observe_lane(self, lane: Lane) -> JsonObject:
        return self.observe_agent()

    def reject_lane(self, lane: Lane, error: ActionRejected) -> None:
        self.reject(error)

    def native_wake(self, lane: Lane) -> int | None:
        adapter = self.adapter_for(lane)
        return adapter.status.next_wake if isinstance(adapter, HarnessAdapter) else None

    def runnable(self, lane: Lane) -> bool:
        now = self.world.now
        if lane.status == "idle":
            wake = self.native_wake(lane)
            return bool(self.notifications(lane)) or (wake is not None and wake <= now)
        if lane.status != "active":
            return False
        block = lane.block
        if block is None:
            return True
        if block.receipt_id is not None:
            return self.receipt_settled(lane, block.receipt_id) or (
                block.until is not None and now >= block.until
            )
        if block.until is not None and now >= block.until:
            return True
        return isinstance(self.adapter_for(lane), HarnessAdapter) and any(
            note["id"] not in block.offered for note in self.notifications(lane)
        )

    def next_lane(self) -> Lane | None:
        for offset in range(len(self.lanes)):
            index = (self.turn_cursor + offset) % len(self.lanes)
            lane = self.lanes[index]
            if self.runnable(lane):
                lane.status = "active"
                lane.block = None
                self.turn_cursor = (index + 1) % len(self.lanes)
                return lane
        return None

    def next_time(self) -> int:
        """The next time anything can happen: an event, reminder or wait timeout, an operator step,
        a lane's wake, a task start or deadline, or the horizon."""
        now = self.world.now
        times = [
            self.config.horizon,
            self.world.next_wake_time(),
            self.schedule.next_time(now),
            self.operator.next_time(now),
        ]
        for lane in self.lanes:
            if lane.status in FINISHED:
                continue
            times.append(lane.block.until if lane.block is not None else None)
            times.append(lane.start_at if lane.status == "pending" else None)
            times.append(lane.deadline)
            times.append(self.native_wake(lane))
        return min((time for time in times if time is not None and time > now), default=now)

    def has_source(self, lane: Lane) -> bool:
        """Whether something can still reach an idle lane before it must end: a world wake, an
        operator step for its chat, a task start, or its native wake."""
        now = self.world.now
        deadline = lane.deadline
        times = [
            self.world.next_wake_time(),
            self.schedule.next_work_time(now, lane.chat_id),
            self.operator.next_work_time(now, self.open_requests()),
            self.native_wake(lane),
            *(other.start_at for other in self.lanes if other.status == "pending"),
        ]
        return any(
            time is not None
            and time > now
            and (time < deadline if deadline is not None else time <= self.config.horizon)
            for time in times
        )

    def complete_quiescent(self) -> bool:
        """End idle lanes as done once no lane is active and nothing can reach any idle one."""
        unfinished = [lane for lane in self.lanes if lane.status not in FINISHED]
        idle = [lane for lane in unfinished if lane.status == "idle"]
        if not idle or any(lane.status == "active" for lane in unfinished):
            return False
        if any(self.has_source(lane) for lane in idle):
            return False
        for lane in idle:
            self.finish_lane(lane, "done")
        return True

    def exchange(self, lane: Lane) -> None:
        self.action_count += 1
        lane.steps += 1
        adapter = self.adapter_for(lane)
        observation = self.observe_lane(lane)
        try:
            action = next_action(adapter, observation)
            self.world.deliver(
                self.actors.agent,
                lane.chat_id,
                delivered_ids(adapter, observation, action),
                "exchange",
            )
            self.act(action, lane)
        except ActionRejected as error:
            self.reject_lane(lane, error)

    def prepare_adapters(self) -> None:
        prepare_harness(self.agent)

    def _activate_due_tasks(self) -> None:
        pass

    def _expire_tasks(self) -> None:
        pass

    def step(self) -> None:
        self.world.advance(self.world.now)
        self.apply_due_operator_steps()
        self._expire_tasks()
        self._activate_due_tasks()
        self.collect_waits()
        if all(lane.status in FINISHED for lane in self.lanes):
            self.status = RunStatus.DONE
            return
        lane = self.next_lane()
        if lane is not None:
            if self.action_count >= self.config.max_steps:
                self.status = RunStatus.MAX_STEPS
            else:
                self.exchange(lane)
            return
        if self.complete_quiescent():
            return
        if self.world.now >= self.config.horizon:
            self.status = RunStatus.HORIZON
            return
        self.world.advance(self.next_time())

    def run(self) -> RunResult:
        self.execute()
        return self.finalize()

    def execute(self) -> None:
        try:
            self.prepare_adapters()
            while self.status == RunStatus.RUNNING:
                self.step()
            if self.status != RunStatus.INVALID:
                self.operator.finish()
        except Exception as error:
            self.record_error(error, "runtime_error")

    def reject(
        self,
        error: ActionRejected,
        *,
        task_id: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        self.rejected_actions += 1
        receipt_id = error.action_id or self._rejection_id()
        self.world.record(
            "action_rejected",
            {"id": receipt_id, "error": str(error)},
            actor_id=self.actors.agent,
            task_id=task_id,
            chat_id=chat_id,
        )
        self.add_receipt(
            {
                "id": receipt_id,
                "invocation_id": receipt_id,
                "status": ResultStatus.FAILED,
                "value": None,
                "error": str(error),
                "time": self.world.now,
            }
        )
        return receipt_id

    def _rejection_id(self) -> str:
        used = {receipt.invocation_id for receipt in self.receipts}
        number = self.rejected_actions
        while f"rejected-{number}" in used:
            number += 1
        return f"rejected-{number}"

    def record_error(self, error: Exception, kind: str) -> None:
        failure = describe_failure(error)
        if self.failure is None or (
            failure["kind"] == FailureKind.INFRASTRUCTURE
            and self.failure["kind"] == FailureKind.AGENT
        ):
            self.failure = failure
        self.status = RunStatus.INVALID
        self.world.record(
            kind,
            {"type": type(error).__name__, "error": str(error)},
            actor_id=self.actors.operator,
        )

    def finalize(
        self,
        *,
        owned: bool = False,
        connector: JsonObject | None = None,
        connector_expected: bool = False,
        connector_invalid: bool = False,
    ) -> RunResult:
        snapshot = self.world.snapshot_for_evaluator()
        snapshot["pending_operator_steps"] = json_value(
            [*self.schedule.pending_kinds(), *self.operator.outstanding(self.open_requests())]
        )
        snapshot["pending_approvals"] = json_value(sorted(self.policy.pending))
        trace = [entry.model_dump(mode="json") for entry in self.world.trace]
        verdict = evaluate(self.config.checks, snapshot, trace, self.status, self.initial_snapshot)
        verdict["failure"] = self.failure
        verdict["rejected_actions"] = self.rejected_actions
        return RunResult(
            self.config.id,
            snapshot,
            trace,
            verdict,
            self.status,
            self.action_count,
            run_provenance(
                self.scenario,
                self.agent,
                self.backend_provenance,
                owned=owned,
                connector=connector,
                connector_expected=connector_expected,
                connector_invalid=connector_invalid,
                operator=self.operator.provenance(),
            ),
            operator_usage=self.operator.usage,
        )


def run_scenario(
    scenario: ResolvedScenario,
    adapter: AgentAdapter | None = None,
    *,
    adapters: Mapping[str, AgentAdapter] | None = None,
    agent_folder: Path | None = None,
    operator_transport: OperatorTransport | None = None,
    capture: bool = False,
    policy_port: int | None = None,
) -> RunResult:
    """Run and grade a scenario, closing only adapters created by this function.

    ``agent_folder`` keeps the agent's sidecars and, for a contained agent, everything it wrote;
    without it they live in a temporary folder. ``operator_transport`` answers for a persona
    operator: a model transport, recorded answers, or a test fake. ``capture`` (training only)
    asks the agent's processes to store their model calls beside their sidecars in
    ``agent_folder``. ``policy_port`` (training only) lets a contained agent reach that one port
    of the host, where the trainer serves its policy, at ``policy_url``."""
    training = capture or policy_port is not None
    if training and (agent_folder is None or adapter is not None or adapters is not None):
        raise AdapterError("training needs an agent folder and adapters this run creates")
    if scenario.config.tasks:
        from overnight.execution.episode import run_episode

        if adapter is not None and adapters is not None:
            raise AdapterError("pass adapter or adapters, not both")
        if adapter is not None:
            if len({task.chat_id for task in scenario.config.tasks}) > 1:
                raise AdapterError("episodes with multiple chats require per-chat adapters")
            adapters = {task.chat_id: adapter for task in scenario.config.tasks}
        return run_episode(
            scenario,
            adapters,
            agent_folder=agent_folder,
            operator_transport=operator_transport,
            capture=capture,
            policy_port=policy_port,
        )
    if adapters is not None:
        raise AdapterError("per-chat adapters require a scenario with tasks")
    if adapter is not None:
        return Trial(scenario, adapter, operator_transport).run()
    if isinstance(scenario.config.agent, (CommandAgent, HarnessAgent)):
        if agent_folder is not None:
            agent_folder.mkdir(mode=0o700)
            return _run_owned(
                scenario, agent_folder / "connector.json", operator_transport, capture, policy_port
            )
        with TemporaryDirectory(prefix="overnight-provenance-") as directory:
            return _run_owned(scenario, Path(directory) / "connector.json", operator_transport)
    return _run_owned(scenario, None, operator_transport)


def _run_owned(
    scenario: ResolvedScenario,
    provenance_path: Path | None,
    operator_transport: OperatorTransport | None,
    capture: bool = False,
    policy_port: int | None = None,
) -> RunResult:
    if capture or policy_port is not None:
        owned = make_adapter(scenario.config.agent, provenance_path, capture, policy_port)
    elif provenance_path is not None:
        owned = make_adapter(scenario.config.agent, provenance_path)
    else:
        owned = make_adapter(scenario.config.agent)
    trial: Trial | None = None
    try:
        trial = Trial(scenario, owned, operator_transport)
        trial.execute()
    finally:
        try:
            owned.close()
        except Exception as error:
            if trial is None:
                raise
            trial.record_error(error, "cleanup_error")
    assert trial is not None
    connector = None
    connector_invalid = False
    if provenance_path is not None:
        try:
            paths = (
                owned.provenance_paths
                if isinstance(owned, (CommandAdapter, PersistentCommandAdapter))
                else [provenance_path]
            )
            connector = read_connector_sidecars(paths)
        except ValueError as error:
            trial.record_error(error, "provenance_error")
            connector_invalid = True
    result = trial.finalize(
        owned=True,
        connector=connector,
        connector_expected=provenance_path is not None,
        connector_invalid=connector_invalid,
    )
    if provenance_path is not None and isinstance(
        owned, (CommandAdapter, PersistentCommandAdapter)
    ):
        result.usage = read_usage(owned.provenance_paths)
    if isinstance(owned, PersistentCommandAdapter):
        result.capture_paths = list(owned.capture_paths)
    return result
