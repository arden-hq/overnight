import json
import os
import subprocess
from pathlib import Path
from typing import Any

import verifiers.v1 as vf
from pydantic import Field


def overnight_python() -> str:
    return os.environ.get("OVERNIGHT_PYTHON", "")


class OvernightError(Exception):
    """Overnight could not run or grade the episode, or the model engine failed under it. The
    rollout errors and a trainer drops it."""


ENGINE_STATUSES = frozenset({408, 429})


def engine_fault(trace: vf.Trace) -> str | None:
    for call in trace.calls:
        error = call.error
        if error is None:
            continue
        status = error.status_code
        if status is None or status >= 500 or status in ENGINE_STATUSES:
            return f"{error.type} (status {status}): {error.message}"
    return None


UNANSWERED = "overnight_unanswered_calls"


def overnight_result(trace: vf.Trace) -> dict[str, Any] | None:
    result = trace.info.get("overnight")
    if result is not None:
        return result
    fault = engine_fault(trace)
    if fault is not None:
        raise OvernightError(f"the model engine failed under the policy: {fault}")
    if trace.info.get(UNANSWERED):
        raise OvernightError(
            f"the rollout stopped (stop {trace.stop_condition}) with "
            f"{trace.info[UNANSWERED]} policy call(s) the harness got no answer to"
        )
    if trace.stop_condition == "agent_timeout":
        return None
    raise OvernightError(
        f"Overnight returned no result for this rollout (stop {trace.stop_condition})"
    )


class OvernightTasksetConfig(vf.TasksetConfig):
    family: Path | None = None
    """A directory written by `overnight generate ... --out DIR` (it holds `family.json`)."""
    held_out: list[Path] = Field(default_factory=list)
    """Hand-written scenario and suite folders the family must not overlap. Default: the
    repository's `examples`, as `overnight.families.split` defaults."""
    python: str = Field(default_factory=overnight_python)
    """Overnight's interpreter. Default: `OVERNIGHT_PYTHON`."""


class OvernightData(vf.TaskData):
    # verifiers needs an opening prompt; Overnight's runner sends the agent's actual messages.
    prompt: str = "An Overnight episode."
    scenario: dict[str, Any]
    """The case's scenario document, with its checks; it never reaches the agent from here."""
    family: str
    seed: int


def has_persona(scenario: dict[str, Any]) -> bool:
    operator = scenario.get("operator")
    return isinstance(operator, dict) and isinstance(operator.get("persona"), dict)


def check_split(python: str, family: Path, held_out: list[Path]) -> None:
    if not python:
        raise ValueError("set OVERNIGHT_PYTHON or --env.taskset.python to Overnight's interpreter")
    command = [python, "-m", "overnight.families.split", str(family)]
    for folder in held_out:
        command += ["--held-out", str(folder)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ValueError(f"split check failed for {family}:\n{done.stderr or done.stdout}")


def family_rows(family: Path) -> list[OvernightData]:
    family = family.resolve()
    index = json.loads((family / "family.json").read_text(encoding="utf-8"))
    rows = []
    for case in index["cases"]:
        path = (family / case["scenario"]).resolve()
        if path.parent != family:
            raise ValueError(f"case {case['id']} names a scenario outside {family}")
        rows.append(
            OvernightData(
                id=case["id"],
                name=case["id"],
                scenario=json.loads(path.read_text(encoding="utf-8")),
                family=index["family"],
                seed=case["seed"],
            )
        )
    return rows


class OvernightTask(vf.Task[OvernightData]):
    """One Overnight episode. Its reward is Overnight's score; an episode the agent under
    training broke, or ran out of time on, scores 0, and one Overnight could not grade never
    reaches scoring."""

    @vf.reward
    async def overnight_score(self, trace: vf.Trace) -> float:
        result = overnight_result(trace)
        if result is None:
            return 0.0
        failure = result["failure"]
        if failure is not None and failure["kind"] == "agent":
            return 0.0
        if result["score"] is None:
            raise OvernightError(f"Overnight returned no score (verdict {result['verdict']})")
        return float(result["score"])

    @vf.metric
    async def overnight_passed(self, trace: vf.Trace) -> float:
        result = overnight_result(trace)
        return float(result is not None and result["verdict"] == "passed")


class OvernightTaskset(vf.Taskset[OvernightTask, OvernightTasksetConfig]):
    def load(self):
        family = self.config.family
        if family is None:
            raise ValueError("set --env.taskset.family to a generated family directory")
        check_split(self.config.python, family, self.config.held_out)
        for data in family_rows(family):
            yield OvernightTask(data, self.config.task)
