from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from overnight.domain.contracts import (
    CommandAgent,
    EpisodeTask,
    HarnessAgent,
    InvokeAction,
    JsonObject,
    OperatorMessage,
    OperatorRequest,
    Receipt,
)
from overnight.domain.values import json_object, json_value
from overnight.domain.vocabulary import RunStatus, Verdict
from overnight.execution.adapters import (
    NO_TRAINING,
    ActionRejected,
    AdapterError,
    AgentAdapter,
    CommandAdapter,
    ManagedAdapter,
    PersistentCommandAdapter,
    ResettableAgentAdapter,
    Training,
    make_adapter,
)
from overnight.execution.evaluation import evaluate
from overnight.execution.harness import (
    HarnessAdapter,
    prepare_harness,
)
from overnight.execution.operator import OperatorTransport
from overnight.execution.provenance import read_connector_sidecars
from overnight.execution.runtime import Lane, LaneStatus, RunResult, Trial
from overnight.execution.scenario import ResolvedScenario
from overnight.execution.usage import read_usage
from overnight.world.engine import TraceSink


@dataclass(kw_only=True)
class TaskState(Lane):
    config: EpisodeTask
    status: LaneStatus = "pending"
    start: int
    reports: list[JsonObject] = field(default_factory=list)
    receipt_ids: list[str] = field(default_factory=list)
    outcome: JsonObject | None = None
    held: list[tuple[str, str]] = field(default_factory=list)

    @property
    def task_id(self) -> str | None:
        return self.config.id

    @property
    def start_at(self) -> int | None:
        return self.start

    @property
    def deadline(self) -> int | None:
        return self.config.deadline


class Episode(Trial):
    def __init__(
        self,
        scenario: ResolvedScenario,
        adapters: Mapping[str, AgentAdapter],
        operator_transport: OperatorTransport | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        if not scenario.config.tasks:
            raise AdapterError("episode requires at least one task")
        missing = {task.chat_id for task in scenario.config.tasks} - adapters.keys()
        if missing:
            raise AdapterError(f"missing adapters for chats: {', '.join(sorted(missing))}")
        for chat_id, adapter in adapters.items():
            if isinstance(adapter, HarnessAdapter):
                adapter.bind_chat(chat_id)
        super().__init__(
            scenario, adapters[scenario.config.tasks[0].chat_id], operator_transport, trace_sink
        )
        self.adapters = dict(adapters)
        self.tasks = cast(list[TaskState], self.lanes)
        self.invocation_owners: dict[str, tuple[str, str]] = {}
        self.causes: dict[str, str | None] = {task.id: None for task in scenario.config.tasks}
        self.chat_epochs: dict[str, int] = {task.chat_id: 0 for task in scenario.config.tasks}
        self.observed_chats: set[str] = set()

    def initial_lanes(self) -> Sequence[Lane]:
        return [
            TaskState(
                config=task,
                chat_id=task.chat_id,
                start=self.config.start_time if task.start_at is None else task.start_at,
            )
            for task in self.config.tasks
        ]

    def adapter_for(self, lane: Lane) -> AgentAdapter:
        assert lane.chat_id is not None
        return self.adapters[lane.chat_id]

    def lane_receipts(self, lane: Lane) -> Sequence[Receipt]:
        receipt_ids = set(cast(TaskState, lane).receipt_ids)
        return [receipt for receipt in self.receipts if receipt.invocation_id in receipt_ids]

    def prepare_adapters(self) -> None:
        for chat_id, adapter in self.adapters.items():
            prepare_harness(
                adapter,
                requires_reset=any(
                    task.chat_id == chat_id and task.memory == "reset" for task in self.config.tasks
                ),
            )

    def _activate_due_tasks(self) -> None:
        for state in self.tasks:
            if state.status != "pending" or state.start > self.world.now:
                continue
            chat_id = state.config.chat_id
            conflict = next(
                (
                    other
                    for other in self.tasks
                    if other.chat_id == chat_id and other.status in {"active", "idle"}
                ),
                None,
            )
            if conflict is not None and conflict.status == "active":
                continue
            if conflict is not None:
                self.finish_lane(conflict, "done")
            if state.config.memory == "reset":
                adapter = self.adapters[chat_id]
                if not isinstance(adapter, ResettableAgentAdapter):
                    raise AdapterError(f"adapter for chat {chat_id} does not support memory reset")
                adapter.reset()
                self.chat_epochs[chat_id] = len(self.messages)
            state.status = "active"
            self.world.open_inbox(self.actors.agent, chat_id)
            self.deliver_operator_message(
                OperatorMessage(
                    kind="message",
                    at=self.world.now,
                    message=state.config.message,
                    task_id=state.config.id,
                    chat_id=chat_id,
                ),
                f"task-{state.config.id}",
            )
            if state.config.memory == "retain":
                for message_id, message in state.held:
                    self.world.post_operator_message(
                        self.actors.agent, chat_id, message_id, message
                    )

    def deliver_operator_message(
        self, step: OperatorMessage, message_id: str, request_id: str | None = None
    ) -> None:
        state = next((state for state in self.tasks if state.config.id == step.task_id), None)
        if state is None or state.status != "pending":
            super().deliver_operator_message(step, message_id, request_id)
            return
        self.record_operator_message(step, message_id, request_id)
        state.held.append((message_id, step.message))

    def transcript_chat(self, request: OperatorRequest) -> str | None:
        return request.chat_id

    def _expire_tasks(self) -> None:
        for state in self.tasks:
            deadline = state.config.deadline
            if deadline is None or self.world.now < deadline:
                continue
            if state.status == "idle":
                self.finish_lane(state, "done")
            elif state.status in {"pending", "active"}:
                self.finish_lane(state, "deadline")

    def finish_lane(self, lane: Lane, status: LaneStatus) -> None:
        super().finish_lane(lane, status)
        self._capture_outcome(cast(TaskState, lane), RunStatus.DONE)

    def _visible_messages(self, chat_id: str) -> list[JsonObject]:
        return [
            message
            for message in self.messages[self.chat_epochs[chat_id] :]
            if message.get("chat_id") == chat_id
        ]

    def observe_task(self, state: TaskState) -> JsonObject:
        receipt_ids = set(state.receipt_ids)
        observation = json_object(
            {
                "time": self.world.now,
                "horizon": self.config.horizon,
                "task": {
                    "id": state.config.id,
                    "chat_id": state.config.chat_id,
                    "deadline": state.config.deadline,
                },
                "context": self.config.agent.context,
                "world": self.world.observe(self.actors.agent, state.config.chat_id),
                "operations": self.world.operations_for(self.actors.agent),
                "messages": self._visible_messages(state.config.chat_id),
                "notifications": self.notifications(state),
                "reports": state.reports,
                "receipts": [
                    receipt.model_dump(mode="json", exclude_unset=True)
                    for receipt in self.receipts
                    if receipt.invocation_id in receipt_ids
                ],
                "policy_revision": self.policy.revision,
            }
        )
        self.world.record(
            "agent_observation",
            observation,
            actor_id=self.actors.agent,
            task_id=state.config.id,
            chat_id=state.config.chat_id,
        )
        return observation

    def observe_lane(self, lane: Lane) -> JsonObject:
        state = cast(TaskState, lane)
        self.observed_chats.add(state.config.chat_id)
        return self.observe_task(state)

    def _bind_invocation(self, state: TaskState, action: InvokeAction) -> InvokeAction:
        if action.task_id not in {None, state.config.id} or action.chat_id not in {
            None,
            state.config.chat_id,
        }:
            raise ActionRejected("action task/chat identity does not match its active turn")
        owner = (state.config.id, state.config.chat_id)
        previous = self.invocation_owners.setdefault(action.id, owner)
        if previous != owner:
            raise ActionRejected(f"invocation ID {action.id} is already owned by another task/chat")
        return action.model_copy(update={"task_id": owner[0], "chat_id": owner[1]})

    def reject_lane(self, lane: Lane, error: ActionRejected) -> None:
        state = cast(TaskState, lane)
        owner = (state.config.id, state.config.chat_id)
        action_id = error.action_id
        if action_id is not None and self.invocation_owners.setdefault(action_id, owner) != owner:
            error.action_id = None
        receipt_id = self.reject(error, task_id=owner[0], chat_id=owner[1])
        self.invocation_owners[receipt_id] = owner
        state.receipt_ids.append(receipt_id)

    def invoke_lane(self, lane: Lane, action: InvokeAction) -> None:
        state = cast(TaskState, lane)
        bound = self._bind_invocation(state, action)
        self.last_cause_id = self.causes[state.config.id]
        before = len(self.receipts)
        self.invoke(bound)
        self.causes[state.config.id] = self.last_cause_id
        for receipt in self.receipts[before:]:
            state.receipt_ids.append(receipt.invocation_id)

    def record_report(self, lane: Lane, message: str) -> None:
        state = cast(TaskState, lane)
        report = json_object({"time": self.world.now, "message": message})
        state.reports.append(report)
        self.reports.append(report)
        self.world.record(
            "agent_report",
            report,
            actor_id=self.actors.agent,
            task_id=state.config.id,
            chat_id=state.config.chat_id,
        )

    def _capture_outcome(
        self, state: TaskState, status: RunStatus, label: str | None = None
    ) -> None:
        snapshot = self.world.snapshot_for_evaluator()
        pending = sorted(
            invocation_id
            for invocation_id in self.policy.pending
            if self.invocation_owners.get(invocation_id) == (state.config.id, state.config.chat_id)
        )
        snapshot["pending_approvals"] = json_value([] if state.status == "deadline" else pending)
        if state.status in {"done", "deadline"}:
            self.policy.retire(pending)
        declared_events = {
            event.id for event in self.config.events if event.invocation.task_id == state.config.id
        }
        snapshot["pending_events"] = json_value(
            [event_id for event_id in self.world.pending_events if event_id in declared_events]
        )
        snapshot["pending_operator_steps"] = []
        trace = [entry.model_dump(mode="json") for entry in self.world.trace]
        task_trace = [entry for entry in trace if entry.get("task_id") == state.config.id]
        state.outcome = json_object(
            {
                "task_id": state.config.id,
                "chat_id": state.config.chat_id,
                "status": label or state.status,
                "completed_at": self.world.now,
                "steps": state.steps,
                "pending_approvals": pending,
                "pending_events": snapshot["pending_events"],
                "verdict": evaluate(
                    state.config.checks,
                    snapshot,
                    task_trace,
                    status,
                    self.initial_snapshot,
                ),
            }
        )

    def finalize(
        self,
        *,
        owned: bool = False,
        connector: JsonObject | None = None,
        connector_expected: bool = False,
        connector_invalid: bool = False,
    ) -> RunResult:
        for state in self.tasks:
            if state.outcome is None:
                self._capture_outcome(state, self.status, str(self.status))
        result = super().finalize(
            owned=owned,
            connector=connector,
            connector_expected=connector_expected,
            connector_invalid=connector_invalid,
        )
        result.task_outcomes = [state.outcome for state in self.tasks if state.outcome is not None]
        verdicts: list[Verdict] = []
        for state, outcome in zip(self.tasks, result.task_outcomes, strict=True):
            checks = state.config.checks
            verdict = outcome["verdict"]
            assert isinstance(verdict, dict)
            if (
                (state.status == "deadline" or not outcome["pending_approvals"])
                and not outcome["pending_events"]
                and not (checks.outcomes or checks.forbidden or checks.collateral)
                and verdict["verdict"] != Verdict.FAILED
            ):
                continue
            verdicts.append(Verdict(verdict["verdict"]))
        global_neutral = (
            self.status == RunStatus.DONE
            and not (
                self.config.checks.outcomes
                or self.config.checks.forbidden
                or self.config.checks.collateral
            )
            and not result.verdict["pending_events"]
            and not result.verdict["pending_operator_steps"]
            and not result.verdict["pending_approvals"]
            and result.verdict["verdict"] != Verdict.FAILED
        )
        if not global_neutral or not verdicts:
            verdicts.append(Verdict(result.verdict["verdict"]))
        graded = [
            verdict
            for verdict in (result.verdict, *(item["verdict"] for item in result.task_outcomes))
            if isinstance(verdict, dict)
        ]
        proven_fail = any(_proven_fail(verdict) for verdict in graded)
        result.verdict["verdict"] = (
            Verdict.FAILED
            if proven_fail
            else next(
                verdict
                for verdict in (Verdict.INVALID, Verdict.FAILED, Verdict.UNKNOWN, Verdict.PASSED)
                if verdict in verdicts
            )
        )
        return result


def _provenance_paths(owned: Mapping[str, ManagedAdapter]) -> list[Path]:
    paths: list[Path] = []
    for adapter in owned.values():
        if isinstance(adapter, (CommandAdapter, PersistentCommandAdapter)):
            paths.extend(adapter.provenance_paths)
    return paths


def _proven_fail(verdict: JsonObject) -> bool:
    forbidden = verdict.get("forbidden_trace_sequences")
    collateral = verdict.get("collateral")
    broken = isinstance(collateral, list) and not all(collateral)
    return broken or (isinstance(forbidden, list) and any(forbidden))


def run_episode(
    scenario: ResolvedScenario,
    adapters: Mapping[str, AgentAdapter] | None = None,
    *,
    agent_folder: Path | None = None,
    operator_transport: OperatorTransport | None = None,
    training: Training = NO_TRAINING,
    trace_sink: TraceSink | None = None,
) -> RunResult:
    if adapters is not None:
        return Episode(scenario, adapters, operator_transport, trace_sink).run()
    if isinstance(scenario.config.agent, (CommandAgent, HarnessAgent)):
        if agent_folder is not None:
            agent_folder.mkdir(mode=0o700)
            return _run_owned_episode(
                scenario, agent_folder, operator_transport, training, trace_sink
            )
        with TemporaryDirectory(prefix="overnight-episode-provenance-") as directory:
            return _run_owned_episode(
                scenario, Path(directory), operator_transport, trace_sink=trace_sink
            )
    return _run_owned_episode(scenario, None, operator_transport, trace_sink=trace_sink)


def _run_owned_episode(
    scenario: ResolvedScenario,
    provenance_root: Path | None,
    operator_transport: OperatorTransport | None,
    training: Training = NO_TRAINING,
    trace_sink: TraceSink | None = None,
) -> RunResult:
    owned: dict[str, ManagedAdapter] = {}
    trial: Episode | None = None
    cleanup_error: Exception | None = None
    try:
        for task in scenario.config.tasks:
            if task.chat_id not in owned:
                path = (
                    provenance_root / f"connector-{len(owned)}.json"
                    if provenance_root is not None
                    else None
                )
                if training.requested:
                    owned[task.chat_id] = make_adapter(scenario.config.agent, path, training)
                elif path is not None:
                    owned[task.chat_id] = make_adapter(scenario.config.agent, path)
                else:
                    owned[task.chat_id] = make_adapter(scenario.config.agent)
        trial = Episode(scenario, owned, operator_transport, trace_sink)
        trial.execute()
    finally:
        for chat_id, adapter in reversed(owned.items()):
            try:
                adapter.close()
            except Exception as error:
                if trial is None:
                    cleanup_error = cleanup_error or error
                else:
                    trial.record_error(
                        AdapterError(f"adapter cleanup failed for chat {chat_id}: {error}"),
                        "cleanup_error",
                    )
    if trial is None and cleanup_error is not None:
        raise cleanup_error
    assert trial is not None
    connector = None
    connector_invalid = False
    paths = _provenance_paths(
        {chat: adapter for chat, adapter in owned.items() if chat in trial.observed_chats}
    )
    if provenance_root is not None:
        try:
            connector = read_connector_sidecars(paths)
        except ValueError as error:
            trial.record_error(error, "provenance_error")
            connector_invalid = True
    result = trial.finalize(
        owned=True,
        connector=connector,
        connector_expected=provenance_root is not None,
        connector_invalid=connector_invalid,
    )
    if provenance_root is not None:
        result.usage = read_usage(paths)
    result.capture_paths = [
        path
        for adapter in owned.values()
        if isinstance(adapter, PersistentCommandAdapter)
        for path in adapter.capture_paths
    ]
    return result
