import hashlib
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from overnight.domain.contracts import JsonObject, ScriptedAgent
from overnight.execution.adapters import (
    AgentAdapter,
    CommandAdapter,
    PersistentCommandAdapter,
    ScriptedAdapter,
)
from overnight.execution.harness import HarnessAdapter
from overnight.execution.scenario import ResolvedScenario

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1] if PACKAGE.parent.name == "src" else None


class CodeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    git_commit: str | None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str | None


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    settings: JsonObject
    uses_runtime_defaults: bool


class ConnectorSidecar(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    implementation: str = Field(min_length=1)
    code: CodeIdentity
    runtime: CodeIdentity | None = None
    model: ModelConfiguration | None = None


def _git(*args: str) -> str:
    assert REPO is not None
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_commit() -> str | None:
    if REPO is None:
        return None
    try:
        commit = _git("rev-parse", "HEAD")
        if not commit:
            return None
        return f"{commit}-dirty" if _git("status", "--porcelain") else commit
    except OSError, subprocess.CalledProcessError:
        return None


def _source_sha256() -> str:
    digest = hashlib.sha256()
    paths = sorted(PACKAGE.rglob("*.py"))
    if REPO is not None:
        paths.extend(
            REPO / name for name in ("pyproject.toml", "uv.lock") if (REPO / name).exists()
        )
    for path in sorted(paths):
        relative = (
            (path.relative_to(REPO) if REPO is not None else path.relative_to(PACKAGE))
            .as_posix()
            .encode()
        )
        contents = path.read_bytes()
        digest.update(f"{len(relative)}:".encode())
        digest.update(relative)
        digest.update(f"{len(contents)}:".encode())
        digest.update(contents)
    return digest.hexdigest()


def _package_version() -> str | None:
    try:
        return version("overnight")
    except PackageNotFoundError:
        return None


def backend_provenance() -> JsonObject:
    return {
        "git_commit": _git_commit(),
        "sha256": _source_sha256(),
        "version": _package_version(),
        "python": sys.version.split()[0],
    }


def read_connector_sidecar(path: Path) -> JsonObject | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as input_file:
            contents = input_file.read(1_048_577)
        if len(contents) > 1_048_576:
            raise ValueError("connector provenance exceeds 1 MiB")
        return ConnectorSidecar.model_validate_json(contents).model_dump(mode="json")
    except ValidationError as error:
        raise ValueError(
            f"invalid connector provenance: validation failed ({error.error_count()} errors)"
        ) from error
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid connector provenance: {type(error).__name__}") from error


def read_connector_sidecars(paths: list[Path]) -> JsonObject | None:
    sidecars = [read_connector_sidecar(path) for path in paths]
    available = [sidecar for sidecar in sidecars if sidecar is not None]
    if available and len(available) != len(sidecars):
        raise ValueError("connector provenance missing for an agent invocation")
    if available and any(sidecar != available[0] for sidecar in available[1:]):
        raise ValueError("connector provenance changed between agent invocations")
    return available[0] if available else None


def run_provenance(
    scenario: ResolvedScenario,
    adapter: AgentAdapter,
    backend: JsonObject,
    *,
    owned: bool,
    connector: JsonObject | None = None,
    connector_expected: bool = False,
    connector_invalid: bool = False,
    operator: JsonObject | None = None,
) -> JsonObject:
    resolved = scenario.to_dict()
    encoded = json.dumps(
        resolved, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    adapter_type = type(adapter)
    adapter_code = None
    if adapter_type in (ScriptedAdapter, CommandAdapter, PersistentCommandAdapter, HarnessAdapter):
        adapter_code = backend
    return {
        "scenario": {
            "resolved": resolved,
            "seed": scenario.config.seed,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        },
        "backend": backend,
        "adapter": {
            "identity": f"{adapter_type.__module__}.{adapter_type.__qualname__}",
            "kind": scenario.config.agent.kind if owned else None,
            "owned": owned,
            "code": adapter_code,
        },
        "isolation": isolation(scenario, adapter),
        "connector": connector,
        "connector_status": connector_status(connector, connector_expected, connector_invalid),
        "model": connector["model"] if connector is not None else None,
        "operator": operator if operator is not None else {"kind": "scripted"},
    }


def isolation(scenario: ResolvedScenario, adapter: AgentAdapter) -> JsonObject | None:
    agent = scenario.config.agent
    if isinstance(agent, ScriptedAgent):
        return None
    if agent.container is None:
        return {"kind": "host"}
    return {
        "kind": "container",
        "image": agent.container.image,
        "image_id": getattr(adapter, "image_id", None),
    }


def connector_status(connector: JsonObject | None, expected: bool, invalid: bool) -> str:
    if invalid:
        return "invalid"
    if connector is not None:
        return "available"
    return "unavailable" if expected else "not_requested"
