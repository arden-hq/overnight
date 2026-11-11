"""Overnight as a verifiers v1 environment: taskset id `overnight-verifiers`.

verifiers discovers the taskset, its harness and its env from `__all__`.
`load_environment` builds the same env from Python.
"""

from pathlib import Path
from typing import Any

from verifiers.v1.utils.loaders import load_environment as _load_environment
from verifiers.v1.utils.loaders import resolve_env_config

from overnight_verifiers.harness import OvernightHarness, OvernightHarnessConfig
from overnight_verifiers.taskset import (
    OvernightData,
    OvernightTask,
    OvernightTaskset,
    OvernightTasksetConfig,
)

__all__ = [
    "OvernightData",
    "OvernightEnv",
    "OvernightEnvConfig",
    "OvernightHarness",
    "OvernightHarnessConfig",
    "OvernightTask",
    "OvernightTaskset",
    "OvernightTasksetConfig",
    "load_environment",
]

# The env's seat defaults look the harness up through this package's `__all__` while the env
# module loads, so the env comes after it.
from overnight_verifiers.env import OvernightEnv, OvernightEnvConfig  # noqa: E402

TASKSET_ID = "overnight-verifiers"


def load_environment(
    family: str | Path,
    *,
    agent: str | Path | None = None,
    taskset: dict[str, Any] | None = None,
    harness: dict[str, Any] | None = None,
    **env: Any,
) -> OvernightEnv:
    """The env over a generated family directory. `agent` is the Overnight agent document that
    names the policy; `taskset`, `harness` and the other keywords are verifiers env config
    (`operator`, `interception`, ...)."""
    harness_config = {"id": TASKSET_ID, "agent": None if agent is None else str(agent)}
    config = resolve_env_config(
        {
            **env,
            "taskset": {"id": TASKSET_ID, "family": str(family), **(taskset or {})},
            "agent": {"harness": {**harness_config, **(harness or {})}},
        }
    )
    environment = _load_environment(config)
    assert isinstance(environment, OvernightEnv)
    return environment
