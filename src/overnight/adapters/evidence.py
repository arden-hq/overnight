import hashlib
import os
from importlib.metadata import version
from pathlib import Path

import acp

from overnight.adapters.config import Launch
from overnight.domain.transport import AGENT_DIRECTORY_VARIABLE, USAGE_PATH_VARIABLE
from overnight.execution.provenance import (
    CodeIdentity,
    ConnectorSidecar,
    backend_provenance,
)
from overnight.execution.usage import NO_CALLS, UNMEASURED, ProcessUsage, readable


def source_identity(root: Path, release: str | None = None) -> CodeIdentity:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()
        digest.update(f"{len(relative)}:".encode() + relative)
        digest.update(f"{len(contents)}:".encode() + contents)
    return CodeIdentity(git_commit=None, sha256=digest.hexdigest(), version=release)


def evidence_directory(path: Path) -> Path:
    """Where a connector keeps its native evidence. A relative path is inside the folder the
    runner gave this process (`AGENT_DIRECTORY_VARIABLE`), which is fresh for every process of
    every run, so one agent document can run in many episodes; outside a run it is relative to
    the working directory. An absolute path is used as given."""
    folder = os.environ.get(AGENT_DIRECTORY_VARIABLE)
    return Path(folder, path) if folder else path


class UsageMeter:
    """Connector-side totals for the current process, saved after every model call."""

    def __init__(self, target: str | None = None) -> None:
        self.target = target if target is not None else os.environ.get(USAGE_PATH_VARIABLE)
        self.usage = NO_CALLS
        self._session_costs: dict[str, float | None] = {}
        self._open_calls = 0

    def begin(self) -> None:
        """Mark a call as sent. Until it is counted or ended the saved totals read unknown,
        so a process killed mid-call, by anyone, never leaves totals that look complete."""
        self._open_calls += 1
        self._write()

    def end(self) -> None:
        """Close a call the provider did not bill, such as a rejected request."""
        self._close()
        self._write()

    def _close(self) -> None:
        self._open_calls = max(0, self._open_calls - 1)

    def add(self, input_tokens: object, output_tokens: object, cost_usd: object) -> None:
        """Count one call. An unknown or unreadable part stays unknown for the whole process."""
        self._close()
        call = ProcessUsage.model_validate(
            {
                "calls": 1,
                **readable(
                    input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd
                ),
            }
        )
        self.usage = self.usage.plus(call)
        self._write()

    def add_unmeasured(self) -> None:
        """Note work that reported no usage, so earlier totals do not read as complete."""
        self._close()
        self.usage = self.usage.plus(UNMEASURED)
        self._write()

    def set_session_cost(self, session_id: str, cost_usd: object) -> None:
        """Record a session's running total, for protocols that report cost that way."""
        known = ProcessUsage.model_validate(readable(cost_usd=cost_usd))
        self._session_costs[session_id] = known.cost_usd
        self._write()

    def _session_cost(self) -> float | None:
        costs = list(self._session_costs.values())
        return None if None in costs else sum(cost for cost in costs if cost is not None)

    def _write(self) -> None:
        if self.target is None:
            return
        usage = UNMEASURED if self._open_calls else self.usage
        if self._session_costs and not self._open_calls:
            base = UNMEASURED if usage.calls == 0 else usage
            usage = base.model_copy(update={"cost_usd": self._session_cost()})
        path = Path(self.target)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(usage.model_dump_json())
        temporary.replace(path)


def write_provenance(factory: str, launch: Launch) -> None:
    target = os.environ.get("OVERNIGHT_PROVENANCE_PATH")
    if target is None:
        return
    backend = backend_provenance()
    sdk = source_identity(Path(acp.__file__).parent, version("agent-client-protocol"))
    code = hashlib.sha256(
        f"{backend['sha256']}:{launch.adapter_code.sha256}:{sdk.sha256}".encode()
    ).hexdigest()
    sidecar = ConnectorSidecar(
        implementation=f"overnight-acp:{factory}",
        code=CodeIdentity.model_validate(
            {"git_commit": backend["git_commit"], "sha256": code, "version": backend["version"]}
        ),
        runtime=launch.runtime_code,
        model=launch.model,
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(sidecar.model_dump_json(indent=2) + "\n")
