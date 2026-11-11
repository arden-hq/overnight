import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    seed: int
    scenario: dict[str, Any]


def has_persona(scenario: dict[str, Any]) -> bool:
    operator = scenario.get("operator")
    return isinstance(operator, dict) and isinstance(operator.get("persona"), dict)


def check_split(python: str, family: Path, held_out: list[Path]) -> None:
    if not python:
        raise ValueError("set OVERNIGHT_PYTHON or `python` to Overnight's interpreter")
    command = [python, "-m", "overnight.families.split", str(family)]
    for folder in held_out:
        command += ["--held-out", str(folder)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ValueError(f"split check failed for {family}:\n{done.stderr or done.stdout}")


def family_cases(python: str, family: Path, held_out: list[Path]) -> list[Case]:
    """The family's cases, only when the split check passes. Every scenario file must sit in the
    family folder, the same files the check read."""
    check_split(python, family, held_out)
    family = family.resolve()
    index = json.loads((family / "family.json").read_text(encoding="utf-8"))
    cases = []
    for case in index["cases"]:
        path = (family / case["scenario"]).resolve()
        if path.parent != family:
            raise ValueError(f"case {case['id']} names a scenario outside {family}")
        scenario = json.loads(path.read_text(encoding="utf-8"))
        cases.append(Case(case["id"], index["family"], case["seed"], scenario))
    return cases
