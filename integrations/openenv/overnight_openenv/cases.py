"""Training cases from a generated scenario family, after Overnight's split check passes."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def overnight_python() -> str:
    return os.environ.get("OVERNIGHT_PYTHON", "")


@dataclass(frozen=True)
class Case:
    """One generated case: its id and scenario document, with its checks. The scenario never
    reaches the agent from here; Overnight's runner decides what the agent sees."""

    id: str
    scenario: dict[str, Any]

    @property
    def has_persona(self) -> bool:
        operator = self.scenario.get("operator")
        return isinstance(operator, dict) and isinstance(operator.get("persona"), dict)


def check_split(python: str, family: Path, held_out: list[Path]) -> None:
    """Overnight's own split check; a family that overlaps the held-out cases loads nothing."""
    if not python:
        raise ValueError("set OVERNIGHT_PYTHON or `python` to Overnight's interpreter")
    command = [python, "-m", "overnight.families.split", str(family)]
    for folder in held_out:
        command += ["--held-out", str(folder)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ValueError(f"split check failed for {family}:\n{done.stderr or done.stdout}")


def family_cases(python: str, family: Path, held_out: list[Path]) -> dict[str, Case]:
    """Every case of a family written by `overnight generate`, by id. Only the scenario files
    `family.json` lists, inside the family folder, are read."""
    family = family.resolve()
    check_split(python, family, held_out)
    index = json.loads((family / "family.json").read_text(encoding="utf-8"))
    cases: dict[str, Case] = {}
    for entry in index["cases"]:
        path = (family / entry["scenario"]).resolve()
        if path.parent != family:
            raise ValueError(f"case {entry['id']} names a scenario outside {family}")
        cases[entry["id"]] = Case(entry["id"], json.loads(path.read_text(encoding="utf-8")))
    if not cases:
        raise ValueError(f"{family} lists no cases")
    return cases
