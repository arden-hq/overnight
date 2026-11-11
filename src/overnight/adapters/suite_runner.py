import fcntl
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import anyio
from pydantic import TypeAdapter

from overnight.adapters.files import replace_private_json, write_private_json
from overnight.adapters.processes import ProcessTree, process_listing, stop_trees
from overnight.domain.contracts import JsonObject
from overnight.execution.container import sweep as sweep_containers
from overnight.execution.evaluation import score
from overnight.execution.scenario import load_json
from overnight.execution.suite import PlannedCell, Suite, SuitePlan, load_suite
from overnight.execution.suite_report import (
    ArmSpend,
    AttemptOutcome,
    SuiteReport,
    summarize_suite,
)
from overnight.execution.usage import NO_CALLS, UNMEASURED, ProcessUsage, total_usage

SUITE_FILE = "suite.json"
REPORT_FILE = "report.json"
INPUTS_DIR = ".inputs"
LOCK_FILE = ".lock"
STOPPED_SUFFIX = ".stopped"
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
WATCH_SECONDS = 1.0
POLL_SECONDS = 0.05
type AttemptCallback = Callable[[str, str, int, int, AttemptOutcome], None]

VERDICT_OUTCOMES: Mapping[str, AttemptOutcome] = {
    "passed": "passed",
    "failed": "failed",
    "unknown": "unknown",
    "invalid": "invalid",
}


def _attempt_dir(out: Path, arm_id: str, case_id: str, attempt: int, retry: int = 0) -> Path:
    suffix = f"-retry-{retry}" if retry else ""
    return out / arm_id / case_id / f"attempt-{attempt}{suffix}"


def _inputs_dir(out: Path, arm_id: str, case_id: str) -> Path:
    return out / INPUTS_DIR / arm_id / case_id


def _freeze_inputs(cell: PlannedCell, out: Path) -> None:
    folder = _inputs_dir(out, cell.arm_id, cell.case_id)
    folder.mkdir(mode=0o700, parents=True)
    write_private_json(folder / "scenario.json", cell.scenario)
    if cell.overrides is not None:
        write_private_json(folder / "overrides.json", cell.overrides)
    if cell.variation is not None:
        write_private_json(folder / "variation.json", cell.variation)


def _run_arguments(out: Path, arm_id: str, case_id: str) -> list[str]:
    folder = _inputs_dir(out, arm_id, case_id)
    arguments = [str(folder / "scenario.json")]
    for option in ("overrides", "variation"):
        if (folder / f"{option}.json").is_file():
            arguments += [f"--{option}", str(folder / f"{option}.json")]
    return arguments


def _ignore_signals() -> None:
    for number in STOP_SIGNALS:
        signal.signal(number, signal.SIG_IGN)


@dataclass
class _Running:
    key: tuple[str, str, int]
    retry: int
    run_dir: Path
    tree: ProcessTree
    log: BinaryIO


def _start_attempt(
    arguments: Sequence[str], run_dir: Path
) -> tuple[subprocess.Popen[bytes], BinaryIO]:
    run_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [sys.executable, "-m", "overnight.cli", "run", *arguments, "--out", str(run_dir)]
    log = run_dir.with_suffix(".log").open("xb")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        log.close()
        raise
    return process, log


def _settle(run: _Running) -> None:
    run.log.close()
    if not (run.run_dir / "verdict.json").is_file():
        run.tree.send(signal.SIGKILL)


def _verdict(run_dir: Path) -> JsonObject | None:
    if not (run_dir / "trace.jsonl").is_file():
        return None
    try:
        return load_json(run_dir / "verdict.json")
    except OSError, ValueError:
        return None


def _failure(verdict: JsonObject) -> JsonObject:
    failure = verdict.get("failure")
    return failure if isinstance(failure, dict) else {}


def _stopped(run_dir: Path) -> bool:
    return run_dir.with_suffix(STOPPED_SUFFIX).exists() and _verdict(run_dir) is None


def attempt_outcome(run_dir: Path) -> AttemptOutcome:
    verdict = _verdict(run_dir)
    if verdict is None:
        started = run_dir.with_suffix(".log").exists() and not _stopped(run_dir)
        return "error" if started else "missing"
    if verdict.get("verdict") == "invalid" and _failure(verdict).get("kind") == "agent":
        return "agent_failed"
    return VERDICT_OUTCOMES.get(str(verdict.get("verdict")), "error")


def attempt_score(run_dir: Path) -> float | None:
    verdict = _verdict(run_dir)
    if verdict is None:
        return None
    path = run_dir / "task-outcomes.json"
    if not path.exists():
        return score(verdict)
    try:
        tasks = TypeAdapter(list[JsonObject]).validate_json(path.read_bytes())
    except OSError, ValueError:
        return None
    return score(verdict, tasks)


def retryable(run_dir: Path) -> bool:
    match attempt_outcome(run_dir):
        case "invalid" | "agent_failed":
            verdict = _verdict(run_dir)
            return verdict is not None and _failure(verdict).get("retryable") is True
        case "error":
            return True
        case _:
            return False


def _tries(out: Path, arm_id: str, case_id: str, attempt: int) -> list[Path]:
    tries: list[Path] = []
    while (
        (run_dir := _attempt_dir(out, arm_id, case_id, attempt, len(tries)))
        .with_suffix(".log")
        .exists()
    ):
        tries.append(run_dir)
    return tries


def _retries(tries: Sequence[Path]) -> int:
    return sum(1 for run_dir in tries[:-1] if not _stopped(run_dir))


def _spend(run_dirs: Sequence[Path]) -> ArmSpend:
    seconds: list[float | None] = []
    usages: list[ProcessUsage] = []
    operator: list[ProcessUsage] = []
    for run_dir in run_dirs:
        try:
            saved = load_json(run_dir / "usage.json")
            taken = saved.pop("seconds")
            operator_saved = saved.pop("operator", None)
            seconds.append(taken if isinstance(taken, (int, float)) else None)
            usages.append(ProcessUsage.model_validate(saved))
            operator.append(
                NO_CALLS
                if operator_saved is None
                else ProcessUsage.model_validate(operator_saved, strict=False)
            )
        except OSError, ValueError, KeyError:
            seconds.append(None)
            usages.append(UNMEASURED)
            operator.append(UNMEASURED)
    usage = total_usage(usages)
    operator_usage = total_usage(operator)
    return ArmSpend(
        seconds=None
        if None in seconds or not seconds
        else sum(s for s in seconds if s is not None),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
        operator_calls=operator_usage.calls,
        operator_cost_usd=operator_usage.cost_usd,
    )


def collect_report(suite: Suite, out: Path) -> SuiteReport:
    outcomes: dict[tuple[str, str], list[AttemptOutcome]] = {}
    scores: dict[tuple[str, str], list[float | None]] = {}
    retries = dict.fromkeys((arm.id for arm in suite.arms), 0)
    runs: dict[str, list[Path]] = {arm.id: [] for arm in suite.arms}
    for arm in suite.arms:
        for case in suite.cases:
            cell = outcomes.setdefault((arm.id, case.id), [])
            score_cell = scores.setdefault((arm.id, case.id), [])
            for attempt in range(1, suite.attempts + 1):
                tries = _tries(out, arm.id, case.id, attempt)
                last = tries[-1] if tries else _attempt_dir(out, arm.id, case.id, attempt)
                cell.append(attempt_outcome(last))
                score_cell.append(attempt_score(last))
                retries[arm.id] += _retries(tries)
                runs[arm.id] += tries
    return summarize_suite(
        suite, outcomes, retries, {arm: _spend(paths) for arm, paths in runs.items()}, scores
    )


def read_report(out: Path) -> SuiteReport:
    return collect_report(load_suite(out / SUITE_FILE), out)


def _next_retry(suite: Suite, out: Path, arm_id: str, case_id: str, attempt: int) -> int | None:
    tries = _tries(out, arm_id, case_id, attempt)
    if not tries:
        return 0
    last = tries[-1]
    if _stopped(last):
        return len(tries)
    if not retryable(last):
        return None
    return len(tries) if _retries(tries) < suite.retries else None


def _run_attempts(suite: Suite, out: Path, on_attempt: AttemptCallback, concurrency: int) -> None:
    queue = deque(
        (arm.id, case.id, attempt)
        for attempt in range(1, suite.attempts + 1)
        for arm in suite.arms
        for case in suite.cases
    )
    running: list[_Running] = []
    next_watch = 0.0
    try:
        while queue or running:
            while queue and len(running) < concurrency:
                key = queue.popleft()
                retry = _next_retry(suite, out, *key)
                if retry is None:
                    continue
                run_dir = _attempt_dir(out, *key, retry)
                blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
                try:
                    process, log = _start_attempt(_run_arguments(out, key[0], key[1]), run_dir)
                    running.append(_Running(key, retry, run_dir, ProcessTree(process), log))
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            if not running:
                continue
            time.sleep(POLL_SECONDS)
            if time.monotonic() >= next_watch:
                listing = process_listing()
                for run in running:
                    run.tree.observe(listing)
                next_watch = time.monotonic() + WATCH_SECONDS
            for run in [run for run in running if run.tree.process.poll() is not None]:
                _settle(run)
                running.remove(run)
                on_attempt(*run.key, run.retry, attempt_outcome(run.run_dir))
                queue.appendleft(run.key)
    except BaseException:
        _ignore_signals()
        for run in running:
            if run.tree.process.poll() is None:
                run.run_dir.with_suffix(STOPPED_SUFFIX).touch()
        stop_trees([run.tree for run in running])
        for run in running:
            run.log.close()
        anyio.run(sweep_containers)
        raise


@contextmanager
def _exclusive(out: Path) -> Generator[None]:
    with (out / LOCK_FILE).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"suite is already running: {out}") from error
        yield


def _finish(suite: Suite, out: Path, on_attempt: AttemptCallback, concurrency: int) -> SuiteReport:
    with _exclusive(out):
        handlers = {number: signal.getsignal(number) for number in STOP_SIGNALS}
        try:
            _run_attempts(suite, out, on_attempt, concurrency)
        except KeyboardInterrupt:
            pass
        finally:
            _ignore_signals()
            try:
                report = collect_report(suite, out)
                replace_private_json(out / REPORT_FILE, report.model_dump(mode="json"), indent=2)
            finally:
                for number, handler in handlers.items():
                    signal.signal(number, handler)
    return report


def run_suite(
    plan: SuitePlan,
    source: JsonObject,
    out: Path,
    on_attempt: AttemptCallback,
    concurrency: int = 1,
) -> SuiteReport:
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private_json(out / SUITE_FILE, source)
    for cell in plan.cells:
        _freeze_inputs(cell, out)
    return _finish(plan.suite, out, on_attempt, concurrency)


def resume_suite(out: Path, on_attempt: AttemptCallback, concurrency: int = 1) -> SuiteReport:
    if not (out / INPUTS_DIR).is_dir():
        raise ValueError(f"not a suite run directory: {out}")
    return _finish(load_suite(out / SUITE_FILE), out, on_attempt, concurrency)
