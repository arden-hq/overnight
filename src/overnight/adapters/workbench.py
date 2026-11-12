import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, JsonValue, ValidationError
from pydantic.json_schema import models_json_schema
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from overnight.adapters.processes import STOP_GRACE_SECONDS, ProcessTree
from overnight.domain.contracts import ScenarioError
from overnight.execution.inspection import (
    InspectionError,
    TraceQuery,
    TraceQueryResult,
    TraceRecord,
    load_trace,
    query_trace,
    trace_lines,
)
from overnight.execution.run_view import (
    LAYERS,
    ApiModel,
    RunArtifacts,
    RunComparison,
    RunDetail,
    ScenarioCheckSet,
    ServiceView,
    build_run_detail,
    compare_runs,
    scenario_checks,
    service_views,
)
from overnight.execution.scenario import resolve_scenario
from overnight.services.catalog import BUILTIN_SERVICES

RUN_ARTIFACTS = frozenset(
    {
        "resolved-scenario.json",
        "trace.jsonl",
        "final-state.json",
        "verdict.json",
        "provenance.json",
        "task-outcomes.json",
        "variation.json",
        "overrides.json",
    }
)
RUN_MARKERS = ("resolved-scenario.json",)
MAX_RUN_DEPTH = 5
MAX_SCENARIO_DEPTH = 3
SKIPPED_SCENARIO_DIRS = frozenset({"fixtures", "adapters", "__pycache__", "node_modules"})
SCENARIO_KEYS = frozenset({"id", "agent", "services", "checks"})
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._%-]+(~[A-Za-z0-9._%-]+)*$")
_DETAIL_CACHE_SIZE = 8
MAX_BODY_BYTES = 8 * 1024 * 1024
OUTPUT_TAIL_BYTES = 16 * 1024
OFFLINE_AGENT_KINDS = frozenset({"scripted"})


class ApiError(ApiModel):
    code: str
    message: str
    details: JsonValue = None


type OperationState = Literal["running", "finished", "failed", "stopped"]


class RunOperation(ApiModel):
    """One `overnight run` process. ``finished`` means the run wrote a verdict, whatever it says."""

    id: str
    scenario: str
    scenario_id: str | None
    run_id: str | None
    out: str
    command: list[str]
    state: OperationState
    stop_requested: bool
    exit_code: int | None
    started: str
    ended: str | None
    stdout_tail: str
    stderr_tail: str


class StatusResponse(ApiModel):
    service: str
    version: str
    runs_roots: list[str]
    scenarios_roots: list[str]
    allow_live: bool
    operation: RunOperation | None


class ScenarioValidation(ApiModel):
    valid: bool
    scenario_id: str
    seed: int


class RunRef(ApiModel):
    id: str
    verdict: str | None
    modified: str


class RunSummary(ApiModel):
    id: str
    name: str
    path: str
    root: int
    modified: str
    scenario_id: str | None
    agent_kind: str | None
    verdict: str | None
    run_status: str | None
    outcomes_passed: int
    outcomes_total: int
    collateral_passed: int
    collateral_total: int
    claims_passed: int
    claims_total: int
    forbidden_hits: int
    model: JsonValue
    adapter_kind: str | None
    connector_status: str | None
    has_tasks: bool
    has_variation: bool
    has_overrides: bool
    complete: bool


class RunList(ApiModel):
    runs: list[RunSummary]
    scanned_seconds: float


class ScenarioSummary(ApiModel):
    id: str
    scenario_id: str | None
    path: str
    valid: bool
    error: str | None
    agent_kind: str | None
    task_count: int
    service_kinds: list[str]
    people: int
    check_counts: dict[str, int]
    etag: str
    last_run: RunRef | None


class ScenarioList(ApiModel):
    scenarios: list[ScenarioSummary]


class ScenarioDetail(ApiModel):
    summary: ScenarioSummary
    scenario: JsonValue
    services: list[ServiceView]
    checks: ScenarioCheckSet


class TraceLine(ApiModel):
    line: int
    entry: JsonValue


RESPONSE_MODELS: tuple[type[BaseModel], ...] = (
    ApiError,
    StatusResponse,
    RunList,
    RunSummary,
    RunDetail,
    ScenarioList,
    ScenarioDetail,
    TraceLine,
    TraceQueryResult,
    ScenarioValidation,
    RunOperation,
    RunComparison,
)


class WorkbenchError(Exception):
    def __init__(self, status: int, code: str, message: str, details: JsonValue = None) -> None:
        super().__init__(message)
        self.status = status
        self.error = ApiError(code=code, message=message, details=details)


def _not_found(what: str, key: str) -> WorkbenchError:
    return WorkbenchError(404, "not_found", f"unknown {what}", {what: key})


def _read_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        return handle.read()


def _read_json(path: Path) -> JsonValue:
    try:
        return json.loads(_read_bytes(path))
    except FileNotFoundError:
        return None
    except OSError, ValueError:
        return None


def _signature(paths: Sequence[Path]) -> tuple[tuple[int, int] | None, ...]:
    signature: list[tuple[int, int] | None] = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            signature.append(None)
            continue
        signature.append((info.st_mtime_ns, info.st_size))
    return tuple(signature)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def encode_id(root: int, parts: Sequence[str], *, plain_root: bool = False) -> str:
    encoded = [quote(part, safe="-._") for part in parts]
    return "~".join(encoded if plain_root and root == 0 else [str(root), *encoded])


def _valid_id(key: str) -> bool:
    return bool(_ID_PATTERN.fullmatch(key)) and ".." not in key.split("~")


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


class _Lru[K, V]:
    def __init__(self, size: int) -> None:
        self.size = size
        self.items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        value = self.items.get(key)
        if value is not None:
            self.items.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> V:
        self.items[key] = value
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)
        return value


@dataclass(frozen=True)
class RunLocation:
    id: str
    root: int
    path: Path
    parts: tuple[str, ...]

    def artifact(self, name: str) -> Path:
        if name not in RUN_ARTIFACTS:
            raise WorkbenchError(404, "not_served", "artifact is not served", {"name": name})
        return self.path / name


def discover_runs(roots: Sequence[Path]) -> dict[str, RunLocation]:
    found: dict[str, RunLocation] = {}

    def walk(root: int, base: Path, path: Path, depth: int) -> None:
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        names = {entry.name for entry in entries if entry.is_file(follow_symlinks=False)}
        if all(marker in names for marker in RUN_MARKERS):
            parts = path.relative_to(base).parts
            if parts:
                location = RunLocation(encode_id(root, parts), root, path, parts)
                found[location.id] = location
            return
        if depth >= MAX_RUN_DEPTH:
            return
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            if entry.is_dir(follow_symlinks=False):
                walk(root, base, Path(entry.path), depth + 1)

    for index, root in enumerate(roots):
        if root.is_dir() and not root.is_symlink():
            walk(index, root, root, 0)
    return found


def _check_counts(verdict: dict[str, JsonValue]) -> dict[str, int]:
    def values(key: str) -> list[JsonValue]:
        found = verdict.get(key)
        return found if isinstance(found, list) else []

    outcomes, collateral, claims = values("outcomes"), values("collateral"), values("claims")
    return {
        "outcomes_passed": sum(1 for value in outcomes if value is True),
        "outcomes_total": len(outcomes),
        "collateral_passed": sum(1 for value in collateral if value is True),
        "collateral_total": len(collateral),
        "claims_passed": sum(1 for value in claims if value is True),
        "claims_total": len(claims),
        "forbidden_hits": sum(1 for value in values("forbidden_trace_sequences") if value),
    }


class RunStore:
    def __init__(self, roots: Sequence[Path]) -> None:
        self.roots = tuple(roots)
        self._locations: dict[str, RunLocation] = {}
        self._summaries: dict[Path, tuple[tuple[object, ...], RunSummary]] = {}
        self._details: _Lru[tuple[str, tuple[object, ...]], RunDetail] = _Lru(_DETAIL_CACHE_SIZE)
        self._traces: _Lru[tuple[str, tuple[object, ...]], tuple[TraceRecord, ...]] = _Lru(
            _DETAIL_CACHE_SIZE
        )

    def refresh(self) -> dict[str, RunLocation]:
        self._locations = discover_runs(self.roots)
        return self._locations

    def locate(self, run_id: str) -> RunLocation:
        if not _valid_id(run_id):
            raise WorkbenchError(400, "invalid_id", "run id is malformed", {"run": run_id})
        location = self._locations.get(run_id) or self.refresh().get(run_id)
        if location is None:
            raise _not_found("run", run_id)
        return location

    def list_runs(self, scenario_id: str | None = None) -> RunList:
        started = time.perf_counter()
        summaries = [self.summary(location) for location in self.refresh().values()]
        if scenario_id is not None:
            summaries = [item for item in summaries if item.scenario_id == scenario_id]
        summaries.sort(key=lambda item: item.modified, reverse=True)
        return RunList(runs=summaries, scanned_seconds=round(time.perf_counter() - started, 4))

    def summary(self, location: RunLocation) -> RunSummary:
        names = (
            "verdict.json",
            "provenance.json",
            "task-outcomes.json",
            "variation.json",
            "overrides.json",
        )
        paths = [location.path, *(location.artifact(name) for name in names)]
        signature = _signature(paths)
        cached = self._summaries.get(location.path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        verdict = _obj(_read_json(location.artifact("verdict.json")))
        provenance = _read_json(location.artifact("provenance.json"))
        resolved = _obj(_obj(_obj(provenance).get("scenario")).get("resolved"))
        if not resolved:
            resolved = _obj(_read_json(location.artifact("resolved-scenario.json")))
        adapter = _obj(_obj(provenance).get("adapter"))
        modified = max((item[0] for item in signature if item is not None), default=0)
        summary = RunSummary(
            id=location.id,
            name=location.parts[-1],
            path=str(location.path),
            root=location.root,
            modified=_iso(modified / 1e9),
            scenario_id=_str(resolved.get("id")),
            agent_kind=_str(_obj(resolved.get("agent")).get("kind")),
            verdict=_str(verdict.get("verdict")),
            run_status=_str(verdict.get("run_status")),
            **_check_counts(verdict),
            model=_obj(provenance).get("model"),
            adapter_kind=_str(adapter.get("kind")),
            connector_status=_str(_obj(provenance).get("connector_status")),
            has_tasks=signature[3] is not None,
            has_variation=signature[4] is not None,
            has_overrides=signature[5] is not None,
            complete=bool(verdict),
        )
        self._summaries[location.path] = (signature, summary)
        return summary

    def _trace_signature(self, location: RunLocation) -> tuple[object, ...]:
        return _signature([location.artifact(name) for name in sorted(RUN_ARTIFACTS)])

    def detail(self, run_id: str) -> RunDetail:
        location = self.locate(run_id)
        key = (location.id, self._trace_signature(location))
        cached = self._details.get(key)
        if cached is not None:
            return cached
        trace, issues = self._raw_trace(location)
        artifacts = RunArtifacts(
            scenario=_read_json(location.artifact("resolved-scenario.json")),
            trace=trace,
            verdict=_read_json(location.artifact("verdict.json")),
            final_state=_read_json(location.artifact("final-state.json")),
            provenance=_read_json(location.artifact("provenance.json")),
            task_outcomes=_read_json(location.artifact("task-outcomes.json")),
            variation=_read_json(location.artifact("variation.json")),
            issues=issues,
        )
        return self._details.put(key, build_run_detail(location.id, artifacts))

    def _raw_trace(self, location: RunLocation) -> tuple[list[tuple[int, JsonValue]], list[str]]:
        try:
            lines = trace_lines(_read_bytes(location.artifact("trace.jsonl")))
        except FileNotFoundError:
            return [], ["no trace recorded: the run did not finish"]
        except (OSError, UnicodeError) as error:
            return [], [f"trace unreadable: {error}"]
        rows: list[tuple[int, JsonValue]] = []
        issues: list[str] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                rows.append((number, json.loads(line)))
            except ValueError as error:
                issues.append(f"trace line {number}: {error}")
        return rows, issues

    def compare(self, a: str, b: str) -> RunComparison:
        trace_a, _ = self._raw_trace(self.locate(a))
        trace_b, _ = self._raw_trace(self.locate(b))
        return compare_runs(self.detail(a), trace_a, self.detail(b), trace_b)

    def records(self, run_id: str) -> tuple[TraceRecord, ...]:
        location = self.locate(run_id)
        key = (location.id, self._trace_signature(location))
        cached = self._traces.get(key)
        if cached is not None:
            return cached
        path = location.artifact("trace.jsonl")
        if path.is_symlink():
            raise WorkbenchError(422, "trace_invalid", "trace is not a regular file")
        try:
            return self._traces.put(key, load_trace(path))
        except InspectionError as error:
            raise WorkbenchError(422, "trace_invalid", str(error)) from error

    def entry(self, run_id: str, line: int) -> TraceLine:
        rows, _ = self._raw_trace(self.locate(run_id))
        for number, entry in rows:
            if number == line:
                return TraceLine(line=number, entry=entry)
        raise _not_found("line", str(line))

    def artifact_bytes(self, run_id: str, name: str) -> bytes:
        path = self.locate(run_id).artifact(name)
        try:
            return _read_bytes(path)
        except OSError as error:
            raise _not_found("artifact", name) from error


@dataclass(frozen=True)
class ScenarioLocation:
    id: str
    path: Path


def discover_scenarios(roots: Sequence[Path]) -> dict[str, ScenarioLocation]:
    found: dict[str, ScenarioLocation] = {}

    def walk(root: int, base: Path, path: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(path), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth < MAX_SCENARIO_DEPTH and entry.name not in SKIPPED_SCENARIO_DIRS:
                    walk(root, base, Path(entry.path), depth + 1)
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".json"):
                parts = Path(entry.path).relative_to(base).with_suffix("").parts
                key = encode_id(root, parts, plain_root=True)
                found[key] = ScenarioLocation(key, Path(entry.path))

    for index, root in enumerate(roots):
        if root.is_dir() and not root.is_symlink():
            walk(index, root, root, 0)
    return found


@dataclass
class _ScenarioEntry:
    signature: tuple[object, ...]
    document: JsonValue
    etag: str
    valid: bool
    error: str | None

    @property
    def scenario_id(self) -> str | None:
        return _str(_obj(self.document).get("id"))


@dataclass
class ScenarioStore:
    roots: Sequence[Path]
    _entries: dict[Path, _ScenarioEntry] = field(default_factory=dict)

    def _load(self, location: ScenarioLocation) -> _ScenarioEntry | None:
        signature = _signature([location.path])
        cached = self._entries.get(location.path)
        if cached is not None and cached.signature == signature:
            return cached
        try:
            raw = _read_bytes(location.path)
            document = json.loads(raw)
        except OSError, ValueError:
            return None
        if not isinstance(document, dict) or not SCENARIO_KEYS <= document.keys():
            return None
        try:
            resolve_scenario(document)
            valid, error = True, None
        except ValueError as problem:
            valid, error = False, str(problem)
        entry = _ScenarioEntry(signature, document, hashlib.sha256(raw).hexdigest(), valid, error)
        self._entries[location.path] = entry
        return entry

    def scenarios(self) -> Iterator[tuple[ScenarioLocation, _ScenarioEntry]]:
        for location in discover_scenarios(self.roots).values():
            entry = self._load(location)
            if entry is not None:
                yield location, entry

    def find(self, scenario_key: str) -> tuple[ScenarioLocation, _ScenarioEntry]:
        if not _valid_id(scenario_key):
            raise WorkbenchError(
                400, "invalid_id", "scenario id is malformed", {"scenario": scenario_key}
            )
        for location, entry in self.scenarios():
            if location.id == scenario_key:
                return location, entry
        raise _not_found("scenario", scenario_key)

    def save(
        self, scenario_key: str, document: dict[str, JsonValue], if_match: str
    ) -> tuple[ScenarioLocation, _ScenarioEntry]:
        location, _ = self.find(scenario_key)
        with _SAVE_LOCK:
            try:
                raw = _read_bytes(location.path)
            except OSError as error:
                raise _not_found("scenario", scenario_key) from error
            current = hashlib.sha256(raw).hexdigest()
            if if_match != current:
                raise WorkbenchError(
                    409,
                    "stale_etag",
                    "the scenario file changed since it was read",
                    {"etag": current},
                )
            check_scenario(document)
            content = scenario_bytes(document, json.loads(raw))
            if content != raw:
                _replace_file(location.path, content)
        entry = self._load(location)
        if entry is None:
            raise WorkbenchError(500, "save_failed", "saved scenario could not be read back")
        return location, entry


_SAVE_LOCK = threading.Lock()


def check_scenario(document: JsonValue) -> ScenarioValidation:
    if not isinstance(document, dict):
        raise WorkbenchError(422, "invalid_scenario", "scenario must be a JSON object")
    try:
        resolved = resolve_scenario(document)
    except ScenarioError as error:
        raise WorkbenchError(422, "invalid_scenario", *_scenario_problem(error)) from error
    return ScenarioValidation(valid=True, scenario_id=resolved.config.id, seed=resolved.config.seed)


def _scenario_problem(error: ScenarioError) -> tuple[str, JsonValue]:
    cause = error.__cause__
    if isinstance(cause, ValidationError):
        problems = [
            f"{'.'.join(str(part) for part in item['loc']) or 'scenario'}: {item['msg']}"
            for item in cause.errors(include_url=False)
        ]
        return problems[0], {"error": "\n".join(problems)}
    message = str(error) or "invalid scenario"
    return message.splitlines()[0], {"error": message}


def _align(new: JsonValue, old: JsonValue) -> JsonValue:
    if isinstance(new, dict) and isinstance(old, dict):
        keys = [key for key in old if key in new] + [key for key in new if key not in old]
        return {key: _align(new[key], old.get(key)) for key in keys}
    if isinstance(new, list) and isinstance(old, list):
        return [_align(item, old[i] if i < len(old) else None) for i, item in enumerate(new)]
    if type(new) is int and type(old) is float and new == old:
        return old
    return new


def scenario_bytes(document: JsonValue, previous: JsonValue = None) -> bytes:
    aligned = _align(document, previous)
    return (json.dumps(aligned, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _replace_file(path: Path, content: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _scenario_summary(
    location: ScenarioLocation, entry: _ScenarioEntry, last_run: RunRef | None
) -> ScenarioSummary:
    document = _obj(entry.document)
    checks = _obj(document.get("checks"))
    actors = document.get("actors")
    services = document.get("services")
    tasks = document.get("tasks")
    return ScenarioSummary(
        id=location.id,
        scenario_id=_str(document.get("id")),
        path=str(location.path),
        valid=entry.valid,
        error=entry.error,
        agent_kind=_str(_obj(document.get("agent")).get("kind")),
        task_count=len(tasks) if isinstance(tasks, list) else 0,
        service_kinds=sorted(
            {kind for item in services if (kind := _str(_obj(item).get("kind")))}
            if isinstance(services, list)
            else set()
        ),
        people=sum(1 for item in actors if _obj(item).get("role") == "person")
        if isinstance(actors, list)
        else 0,
        check_counts={
            key: len(value) if isinstance(value := checks.get(key), list) else 0
            for _, key, _ in LAYERS
        },
        etag=entry.etag,
        last_run=last_run,
    )


class _Tail:
    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, stream: Any) -> None:
        for chunk in iter(lambda: stream.read1(4096), b""):
            self.data.extend(chunk)
            del self.data[:-OUTPUT_TAIL_BYTES]
        stream.close()

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


@dataclass
class _Operation:
    id: str
    scenario: str
    scenario_id: str | None
    run_id: str
    out: Path
    command: list[str]
    process: subprocess.Popen[bytes]
    started: float
    stdout: _Tail = field(default_factory=_Tail)
    stderr: _Tail = field(default_factory=_Tail)
    ended: float | None = None
    stop_requested: bool = False
    frozen: Path | None = None
    tree: ProcessTree = field(init=False)

    def __post_init__(self) -> None:
        self.tree = ProcessTree(self.process)

    def send(self, sig: signal.Signals) -> None:
        self.tree.send(sig)

    def view(self) -> RunOperation:
        code = self.process.poll()
        if code is None:
            state: OperationState = "running"
        elif (self.out / "verdict.json").is_file():
            state = "finished"
        else:
            state = "stopped" if self.stop_requested else "failed"
        known = (self.out / RUN_MARKERS[0]).is_file()
        return RunOperation(
            id=self.id,
            scenario=self.scenario,
            scenario_id=self.scenario_id,
            run_id=self.run_id if known else None,
            out=str(self.out),
            command=self.command,
            state=state,
            stop_requested=self.stop_requested,
            exit_code=code,
            started=_iso(self.started),
            ended=_iso(self.ended) if self.ended is not None else None,
            stdout_tail=self.stdout.text(),
            stderr_tail=self.stderr.text(),
        )


class Operations:
    def __init__(self, stop_grace: float = STOP_GRACE_SECONDS) -> None:
        self._items: OrderedDict[str, _Operation] = OrderedDict()
        self._lock = threading.Lock()
        self.stop_grace = stop_grace

    def active(self) -> _Operation | None:
        last = next(reversed(self._items.values()), None)
        return last if last is not None and last.process.poll() is None else None

    def get(self, operation_id: str) -> _Operation:
        found = self._items.get(operation_id)
        if found is None:
            raise _not_found("operation", operation_id)
        return found

    def start(
        self,
        command: list[str],
        *,
        out: Path,
        run_id: str,
        scenario: str,
        scenario_id: str | None,
        frozen: Path | None = None,
    ) -> _Operation:
        with self._lock:
            busy = self.active()
            if busy is not None:
                raise WorkbenchError(
                    409, "busy", "another run is in progress", {"operation": busy.id}
                )
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            started = time.time()
            operation = _Operation(
                id=f"op-{int(started * 1000):x}",
                scenario=scenario,
                scenario_id=scenario_id,
                run_id=run_id,
                out=out,
                command=command,
                process=process,
                started=started,
                frozen=frozen,
            )
            self._items[operation.id] = operation
            while len(self._items) > 20:
                self._items.popitem(last=False)
        assert process.stdout is not None and process.stderr is not None
        for tail, stream in (
            (operation.stdout, process.stdout),
            (operation.stderr, process.stderr),
        ):
            threading.Thread(target=tail.feed, args=(stream,), daemon=True).start()

        def wait() -> None:
            process.wait()
            if frozen is not None:
                shutil.rmtree(frozen, ignore_errors=True)
            operation.ended = time.time()

        threading.Thread(target=wait, daemon=True).start()
        return operation

    def stop(self, operation_id: str) -> _Operation:
        operation = self.get(operation_id)
        process = operation.process
        if process.poll() is not None:
            return operation
        operation.stop_requested = True
        operation.send(signal.SIGTERM)

        def escalate() -> None:
            try:
                process.wait(self.stop_grace)
            except subprocess.TimeoutExpired:
                pass
            operation.send(signal.SIGKILL)

        threading.Thread(target=escalate, daemon=True).start()
        return operation

    def close(self) -> None:
        running = [item for item in self._items.values() if item.process.poll() is None]
        for operation in running:
            operation.stop_requested = True
            operation.send(signal.SIGTERM)
        deadline = time.monotonic() + self.stop_grace
        for operation in running:
            try:
                operation.process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            operation.send(signal.SIGKILL)


def _run_dir_name(scenario_id: str, root: Path) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name, suffix = f"{scenario_id}-{stamp}", 1
    while (root / name).exists():
        suffix += 1
        name = f"{scenario_id}-{stamp}-{suffix}"
    return name


@dataclass(frozen=True)
class WorkbenchConfig:
    runs_roots: Sequence[Path]
    scenarios_roots: Sequence[Path]
    allow_live: bool = False
    version: str = "0.1.0"


def _json(model: BaseModel, status: int = 200) -> Response:
    return Response(model.model_dump_json(), status_code=status, media_type="application/json")


def _int_param(request: Request, name: str) -> int | None:
    value = request.query_params.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise WorkbenchError(400, "bad_request", f"{name} must be an integer") from error


def _trace_query(request: Request) -> TraceQuery:
    params = request.query_params
    try:
        return TraceQuery(
            kind=params.get("kind"),
            task_id=params.get("task"),
            chat_id=params.get("chat"),
            actor_id=params.get("actor"),
            invocation_id=params.get("invocation"),
            event_id=params.get("event"),
            time_from=_int_param(request, "from"),
            time_to=_int_param(request, "to"),
            limit=_int_param(request, "limit"),
        )
    except ValidationError as error:
        detail = error.errors(include_url=False)[0]
        raise WorkbenchError(400, "bad_request", str(detail["msg"])) from error


def _reject_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


async def _body(request: Request) -> dict[str, JsonValue]:
    too_large = WorkbenchError(413, "too_large", "request body is too large")
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.strip().isdigit():
            raise WorkbenchError(400, "bad_request", "Content-Length must be an integer")
        if int(declared) > MAX_BODY_BYTES:
            raise too_large
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            raise too_large
    raw = bytes(received)
    try:
        body = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except ValueError as error:
        raise WorkbenchError(400, "bad_request", f"request body is not JSON: {error}") from error
    if not isinstance(body, dict):
        raise WorkbenchError(400, "bad_request", "request body must be a JSON object")
    return body


def _document(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    document = body.get("scenario")
    if not isinstance(document, dict):
        raise WorkbenchError(422, "invalid_scenario", "scenario must be a JSON object")
    return document


def _if_match(request: Request) -> str:
    value = request.headers.get("if-match")
    if not value:
        raise WorkbenchError(428, "etag_required", "If-Match with the scenario ETag is required")
    return value.removeprefix("W/").strip().strip('"')


class WorkbenchApi:
    def __init__(self, config: WorkbenchConfig) -> None:
        self.config = config
        self.runs = RunStore(config.runs_roots)
        self.scenarios = ScenarioStore(config.scenarios_roots)
        self.operations = Operations()

    def _last_runs(self) -> dict[str, RunRef]:
        latest: dict[str, RunRef] = {}
        for run in self.runs.list_runs().runs:
            if run.has_variation or run.has_overrides:
                continue
            if run.scenario_id is not None and run.scenario_id not in latest:
                latest[run.scenario_id] = RunRef(
                    id=run.id, verdict=run.verdict, modified=run.modified
                )
        return latest

    async def status(self, _: Request) -> Response:
        config = self.config
        return _json(
            StatusResponse(
                service="overnight-workbench",
                version=config.version,
                runs_roots=[str(path) for path in config.runs_roots],
                scenarios_roots=[str(path) for path in config.scenarios_roots],
                allow_live=config.allow_live,
                operation=active.view() if (active := self.operations.active()) else None,
            )
        )

    async def list_scenarios(self, _: Request) -> Response:
        latest = self._last_runs()
        return _json(
            ScenarioList(
                scenarios=[
                    _scenario_summary(location, entry, latest.get(entry.scenario_id or ""))
                    for location, entry in self.scenarios.scenarios()
                ]
            )
        )

    def _scenario_detail(self, location: ScenarioLocation, entry: _ScenarioEntry) -> Response:
        latest = self._last_runs().get(entry.scenario_id or "")
        response = _json(
            ScenarioDetail(
                summary=_scenario_summary(location, entry, latest),
                scenario=entry.document,
                services=service_views(entry.document, BUILTIN_SERVICES),
                checks=scenario_checks(entry.document),
            )
        )
        response.headers["ETag"] = f'"{entry.etag}"'
        return response

    async def get_scenario(self, request: Request) -> Response:
        return self._scenario_detail(*self.scenarios.find(request.path_params["scenario_id"]))

    async def validate_scenario(self, request: Request) -> Response:
        self.scenarios.find(request.path_params["scenario_id"])
        return _json(check_scenario(_document(await _body(request))))

    async def save_scenario(self, request: Request) -> Response:
        key = request.path_params["scenario_id"]
        etag = _if_match(request)
        document = _document(await _body(request))
        return self._scenario_detail(*self.scenarios.save(key, document, etag))

    def _input_file(self, body: dict[str, JsonValue], name: str) -> Path | None:
        key = body.get(name)
        if key is None:
            return None
        if not isinstance(key, str) or not _valid_id(key):
            raise WorkbenchError(400, "bad_request", f"{name} must be a scenario-root file key")
        location = discover_scenarios(self.config.scenarios_roots).get(key)
        if location is None:
            raise _not_found(name, key)
        return location.path

    async def start_run(self, request: Request) -> Response:
        body = await _body(request)
        key = body.get("scenario")
        if not isinstance(key, str):
            raise WorkbenchError(400, "bad_request", "scenario must be a scenario key")
        location, _ = self.scenarios.find(key)
        overrides = self._input_file(body, "overrides")
        variation = self._input_file(body, "variation")
        if not self.config.runs_roots:
            raise WorkbenchError(409, "no_runs_root", "the server has no runs root to write to")
        with _SAVE_LOCK:
            try:
                raw = _read_bytes(location.path)
                document = json.loads(raw)
            except (OSError, ValueError) as error:
                raise WorkbenchError(
                    422, "invalid_scenario", "scenario file is unreadable"
                ) from error
            kind = _str(_obj(_obj(document).get("agent")).get("kind"))
            persona = _obj(_obj(document).get("operator")).get("persona") is not None
            if (kind not in OFFLINE_AGENT_KINDS or persona) and not self.config.allow_live:
                raise WorkbenchError(
                    403,
                    "live_refused",
                    f"only scripted runs start here; {kind or 'this'} agents and persona "
                    "operators may call a live model and need overnight serve --allow-live",
                    {"scenario": key, "agent_kind": kind},
                )
            scenario_id = check_scenario(document).scenario_id
            root = Path(self.config.runs_roots[0]).absolute()
            root.mkdir(parents=True, exist_ok=True)
            name = _run_dir_name(re.sub(r"[^A-Za-z0-9._-]", "-", scenario_id), root)
            out = root / name
            frozen = Path(tempfile.mkdtemp(prefix="overnight-workbench-"))
            scenario_copy = frozen / location.path.name
            scenario_copy.write_bytes(raw)
            command = [str(Path(sys.prefix) / "bin" / "overnight"), "run", str(scenario_copy)]
            for flag, path in (("--overrides", overrides), ("--variation", variation)):
                if path is not None:
                    command += [flag, str(path.absolute())]
            command += ["--out", str(out)]
            try:
                operation = self.operations.start(
                    command,
                    out=out,
                    run_id=encode_id(0, (name,)),
                    scenario=location.id,
                    scenario_id=scenario_id,
                    frozen=frozen,
                )
            except BaseException:
                shutil.rmtree(frozen, ignore_errors=True)
                raise
        return _json(operation.view(), 202)

    async def get_operation(self, request: Request) -> Response:
        return _json(self.operations.get(request.path_params["operation_id"]).view())

    async def stop_operation(self, request: Request) -> Response:
        return _json(self.operations.stop(request.path_params["operation_id"]).view())

    async def compare(self, request: Request) -> Response:
        a, b = request.query_params.get("a"), request.query_params.get("b")
        if not a or not b:
            raise WorkbenchError(400, "bad_request", "compare needs run ids a and b")
        return _json(self.runs.compare(a, b))

    async def list_runs(self, request: Request) -> Response:
        return _json(self.runs.list_runs(request.query_params.get("scenario")))

    async def get_run(self, request: Request) -> Response:
        return _json(self.runs.detail(request.path_params["run_id"]))

    async def get_trace(self, request: Request) -> Response:
        query = _trace_query(request)
        return _json(query_trace(self.runs.records(request.path_params["run_id"]), query))

    async def get_entry(self, request: Request) -> Response:
        params = request.path_params
        return _json(self.runs.entry(params["run_id"], params["line"]))

    async def get_artifact(self, request: Request) -> Response:
        name = request.path_params["name"]
        body = self.runs.artifact_bytes(request.path_params["run_id"], name)
        media = "application/x-ndjson" if name.endswith(".jsonl") else "application/json"
        return Response(body, media_type=media)

    def routes(self) -> list[Route]:
        return [
            Route("/api/status", self.status),
            Route("/api/scenarios", self.list_scenarios),
            Route("/api/scenarios/{scenario_id}", self.get_scenario, methods=["GET"]),
            Route("/api/scenarios/{scenario_id}", self.save_scenario, methods=["PUT"]),
            Route(
                "/api/scenarios/{scenario_id}/validate", self.validate_scenario, methods=["POST"]
            ),
            Route("/api/runs", self.list_runs, methods=["GET"]),
            Route("/api/runs", self.start_run, methods=["POST"]),
            Route("/api/operations/{operation_id}", self.get_operation),
            Route("/api/operations/{operation_id}/stop", self.stop_operation, methods=["POST"]),
            Route("/api/compare", self.compare),
            Route("/api/runs/{run_id}", self.get_run),
            Route("/api/runs/{run_id}/trace", self.get_trace),
            Route("/api/runs/{run_id}/entries/{line:int}", self.get_entry),
            Route("/api/runs/{run_id}/artifacts/{name}", self.get_artifact),
        ]


async def _workbench_error(_: Request, error: Exception) -> Response:
    assert isinstance(error, WorkbenchError)
    return _json(error.error, error.status)


async def _http_error(_: Request, error: Exception) -> Response:
    assert isinstance(error, HTTPException)
    code = "not_found" if error.status_code == 404 else "http_error"
    return _json(ApiError(code=code, message=str(error.detail)), error.status_code)


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(value: str) -> str:
    try:
        return (urlsplit(value if "://" in value else f"//{value}").hostname or "").lower()
    except ValueError:
        return ""


class _LocalOnly:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            problem = None
            if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
                problem = "requests must address 127.0.0.1 or localhost"
            elif request.method in _MUTATING:
                origin = request.headers.get("origin")
                content = request.headers.get("content-type", "").split(";")[0].strip()
                if origin is not None and _hostname(origin) not in LOCAL_HOSTS:
                    problem = "cross-origin writes are refused"
                elif content != "application/json":
                    problem = "writes must send Content-Type: application/json"
            if problem is not None:
                await _json(ApiError(code="forbidden", message=problem), 403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(config: WorkbenchConfig) -> Starlette:
    handlers: Mapping[Any, Callable[..., Any]] = {
        WorkbenchError: _workbench_error,
        HTTPException: _http_error,
    }
    api = WorkbenchApi(config)
    app = Starlette(
        routes=api.routes(),
        exception_handlers=dict(handlers),
        middleware=[Middleware(_LocalOnly)],
    )
    app.state.workbench = api
    return app


def response_schema() -> JsonValue:
    _, schema = models_json_schema(
        [(model, "serialization") for model in RESPONSE_MODELS],
        title="OvernightWorkbenchApi",
        ref_template="#/$defs/{model}",
    )
    schema["type"] = "object"
    schema["properties"] = {
        model.__name__: {"$ref": f"#/$defs/{model.__name__}"} for model in RESPONSE_MODELS
    }
    return schema
