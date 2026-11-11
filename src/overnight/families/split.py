"""Check that generated family cases and the hand-written held-out cases do not overlap.

Generated families train; the hand-written scenarios and suites stay held out for eval. This
fails when a family id or a generated case id names a held-out scenario, suite, suite case or
variation, when a held-out id is named like a case of the family, or when a generated scenario
is a held-out scenario under another name (same document apart from its id, seed and agent).
It also fails when either side has nothing to compare: no held-out scenario, or a family with no
cases. It catches shared ids and exact copies only; a near copy (any other field changed) passes.

    .venv/bin/python -m overnight.families.split .local/families/order-resolution

`scripts/check_split.py` runs the same command.
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import TypeGuard

import click

import overnight

# The repository checkout that holds the hand-written scenarios.
ROOT = Path(overnight.__file__).resolve().parents[2]
IGNORED_KEYS = frozenset({"id", "seed", "agent"})


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise click.ClickException(f"cannot read {path}: {error}") from error


def _is_scenario(document: object) -> TypeGuard[dict]:
    return isinstance(document, dict) and "actors" in document and "services" in document


def _fingerprint(scenario: dict) -> str:
    body = {key: value for key, value in scenario.items() if key not in IGNORED_KEYS}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def held_out(roots: list[Path]) -> tuple[dict[str, Path], dict[str, Path]]:
    """Every id a held-out file names, and the fingerprint of every held-out scenario, including
    scenarios a suite names outside the roots."""
    ids: dict[str, Path] = {}
    prints: dict[str, Path] = {}
    files = [path for root in roots for path in sorted(root.rglob("*.json"))]
    seen: set[Path] = set()
    while files:
        path = files.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        document = _read(path)
        if not isinstance(document, dict):
            continue
        if isinstance(document.get("id"), str):
            ids.setdefault(document["id"], path)
        if _is_scenario(document):
            prints.setdefault(_fingerprint(document), path)
        cases = document.get("cases") if "arms" in document else None
        for case in cases if isinstance(cases, list) else []:
            if not isinstance(case, dict):
                continue
            if isinstance(case.get("id"), str):
                ids.setdefault(case["id"], path)
            for key in ("scenario", "variation"):
                if isinstance(case.get(key), str):
                    files.append(path.parent / case[key])
    return ids, prints


def overlaps(generated: Path, ids: dict[str, Path], prints: dict[str, Path]) -> list[str]:
    index = _read(generated / "family.json")
    if not isinstance(index, dict) or not isinstance(index.get("family"), str):
        raise click.ClickException(f"{generated}/family.json names no family")
    family = index["family"]
    cases = index.get("cases")
    if not isinstance(cases, list) or not cases:
        raise click.ClickException(f"{generated}/family.json has no cases to check")
    found: list[str] = []
    if family in ids:
        found.append(f"family {family} is a held-out id in {ids[family]}")
    for held, path in ids.items():
        if held.startswith(f"{family}-"):
            found.append(f"held-out id {held} in {path} is named like a {family} case")
    for case in cases:
        scenario = _read(generated / case["scenario"])
        if not _is_scenario(scenario):
            raise click.ClickException(f"{generated / case['scenario']} is not a scenario")
        for name in {case["id"], scenario["id"]}:
            if name in ids:
                found.append(f"case {name} is a held-out id in {ids[name]}")
        fingerprint = _fingerprint(scenario)
        if fingerprint in prints:
            found.append(f"case {case['id']} is the held-out scenario {prints[fingerprint]}")
    return found


@click.command()
@click.argument("generated", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option(
    "--held-out",
    "roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Folder of hand-written scenarios and suites; repeatable. Default: examples.",
)
def main(generated: tuple[Path, ...], roots: tuple[Path, ...]) -> None:
    """Fail when a GENERATED family folder overlaps the held-out cases."""
    ids, prints = held_out(list(roots) or [ROOT / "examples"])
    if not prints:
        raise click.ClickException("the held-out set has no scenarios to check against")
    found = [line for folder in generated for line in overlaps(folder, ids, prints)]
    if found:
        click.echo("SPLIT VIOLATION: generated training cases overlap the held-out set", err=True)
        for line in found:
            click.echo(f"  {line}", err=True)
        sys.exit(1)
    click.echo(
        f"split ok: {len(generated)} generated folder(s), {len(ids)} held-out ids, "
        f"{len(prints)} held-out scenarios"
    )


if __name__ == "__main__":
    main()
