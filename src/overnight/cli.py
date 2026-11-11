import errno
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

import anyio
import click
from pydantic import JsonValue, ValidationError

from overnight.adapters.http import serve_http
from overnight.adapters.operator_model import ModelTransport
from overnight.adapters.suite_runner import read_report, resume_suite, run_suite
from overnight.adapters.workbench import WorkbenchConfig, create_app, response_schema
from overnight.domain.values import json_value
from overnight.domain.vocabulary import Verdict
from overnight.execution.inspection import InspectionError, TraceQuery, inspect_trace
from overnight.execution.operator import (
    OperatorTransport,
    OperatorUnavailable,
    RecordedTransport,
)
from overnight.execution.qa import (
    AuthoredProbe,
    ProbeFile,
    check_scenario,
    check_suite,
    qa_exit_code,
    render_qa,
)
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import ResolvedScenario, load_json
from overnight.execution.suite import Suite, load_suite, plan_suite
from overnight.execution.suite_report import SuiteReport, render_report
from overnight.execution.variations import VariationConfig, resolve_variant
from overnight.families import FAMILIES, SEEDS, Case, FamilyError, generate

INFRASTRUCTURE_EXIT_CODE = 4
USAGE_EXIT_CODE = 64


class InfrastructureError(click.ClickException):
    exit_code = INFRASTRUCTURE_EXIT_CODE

    def __init__(self, error: BaseException) -> None:
        super().__init__(f"runtime error: {type(error).__name__}: {error}")


class InvalidInputError(click.ClickException):
    exit_code = 3


def _load_probes(path: Path) -> list[AuthoredProbe]:
    try:
        return ProbeFile.model_validate(load_json(path)).probes
    except ValueError as error:
        raise InvalidInputError(f"invalid probes file {path}: {error}") from error


def _check_qa_files(path: Path, suite: Suite) -> None:
    for case in suite.cases:
        if case.probes is not None:
            _load_probes(path.parent / case.probes)
        if case.operator_answers is not None:
            answers = path.parent / case.operator_answers
            try:
                RecordedTransport.load(answers)
            except ValueError as error:
                raise InvalidInputError(
                    f"invalid operator answers file {answers}: {error}"
                ) from error


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)


def _write_json(path: Path, value: JsonValue) -> None:
    _write_private(
        path,
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
    )


def _resolve_input(
    scenario: Path, overrides: Path | None, variation: VariationConfig | None
) -> ResolvedScenario:
    return resolve_variant(
        load_json(scenario), load_json(overrides) if overrides is not None else None, variation
    )


def _operator_transport(
    resolved: ResolvedScenario, answers: Path | None
) -> OperatorTransport | None:
    persona = resolved.config.operator.persona
    if persona is None:
        return None
    if answers is not None:
        return RecordedTransport.load(answers)
    return ModelTransport(persona.model, resolved.config.seed)


OPERATOR_ANSWERS = click.option(
    "--operator-answers",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Replay the operator persona from a recorded trace.jsonl instead of calling its model.",
)


@click.group()
def cli() -> None:
    """Validate or run an Overnight scenario."""


@cli.command()
@click.argument("scenario", type=click.Path(path_type=Path))
@click.option("--overrides", type=click.Path(path_type=Path))
@click.option("--variation", type=click.Path(path_type=Path))
def validate(scenario: Path, overrides: Path | None, variation: Path | None) -> int:
    """Resolve a scenario and validate all contracts."""
    variation_config = (
        VariationConfig.model_validate(load_json(variation)) if variation is not None else None
    )
    resolved = _resolve_input(scenario, overrides, variation_config)
    click.echo(json.dumps({"valid": True, "id": resolved.config.id, "seed": resolved.config.seed}))
    return 0


@cli.command()
@click.argument("scenario", type=click.Path(path_type=Path))
@click.option("--overrides", type=click.Path(path_type=Path))
@click.option("--variation", type=click.Path(path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), required=True)
@OPERATOR_ANSWERS
def run(
    scenario: Path,
    overrides: Path | None,
    variation: Path | None,
    out: Path,
    operator_answers: Path | None,
) -> int:
    """Run a scenario and save its private evidence in a new directory."""
    variation_config = (
        VariationConfig.model_validate(load_json(variation)) if variation is not None else None
    )
    resolved = _resolve_input(scenario, overrides, variation_config)
    if resolved.config.operator.persona is None and operator_answers is not None:
        raise click.UsageError("--operator-answers needs a scenario with an operator persona")
    try:
        operator_transport = _operator_transport(resolved, operator_answers)
    except OperatorUnavailable as error:
        raise click.UsageError(f"{error}; set it or pass --operator-answers") from error
    if out.exists():
        raise click.UsageError(f"--out {out} already exists")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    _write_json(out / "resolved-scenario.json", resolved.to_dict())
    if overrides is not None:
        _write_json(out / "overrides.json", load_json(overrides))
    if variation_config is not None:
        _write_json(out / "variation.json", variation_config.model_dump(mode="json"))
    started = time.monotonic()
    try:
        result = run_scenario(
            resolved,
            agent_folder=out / "agent",
            operator_transport=operator_transport,
        )
        _write_json(
            out / "usage.json",
            {
                "seconds": time.monotonic() - started,
                **result.usage.model_dump(mode="json"),
                "operator": result.operator_usage.model_dump(mode="json"),
            },
        )
        _write_json(out / "final-state.json", result.final_state)
        _write_json(out / "provenance.json", result.provenance)
        if result.task_outcomes is not None:
            _write_json(out / "task-outcomes.json", json_value(result.task_outcomes))
        _write_private(
            out / "trace.jsonl",
            "".join(
                json.dumps(entry, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
                for entry in result.trace
            ),
        )
        _write_json(out / "verdict.json", result.verdict)
    except Exception as error:
        raise InfrastructureError(error) from error
    click.echo(
        json.dumps(
            {"id": result.scenario_id, "verdict": result.verdict["verdict"], "out": str(out)}
        )
    )
    exit_codes = {Verdict.PASSED: 0, Verdict.FAILED: 1, Verdict.UNKNOWN: 2, Verdict.INVALID: 3}
    return exit_codes[Verdict(str(result.verdict["verdict"]))]


@cli.command()
@click.argument("scenario", type=click.Path(path_type=Path))
@click.option("--overrides", type=click.Path(path_type=Path))
@click.option("--variation", type=click.Path(path_type=Path))
@click.option(
    "--allow-command", is_flag=True, help="Run a command agent as the oracle; it may call a model."
)
@OPERATOR_ANSWERS
@click.option(
    "--probes",
    type=click.Path(path_type=Path),
    help="Wrong agents the scenario must also reject, as written by `overnight generate`.",
)
def qa(
    scenario: Path,
    overrides: Path | None,
    variation: Path | None,
    allow_command: bool,
    operator_answers: Path | None,
    probes: Path | None,
) -> int:
    """Prove a scenario passes its oracle and rejects agents that do no work."""
    authored = _load_probes(probes) if probes is not None else []
    report = check_scenario(
        scenario.stem,
        load_json(scenario),
        load_json(overrides) if overrides is not None else None,
        VariationConfig.model_validate(load_json(variation)) if variation is not None else None,
        allow_command=allow_command,
        operator_answers=operator_answers,
        authored=authored,
    )
    click.echo(render_qa([report]))
    return qa_exit_code([report])


@cli.command("generate")
@click.argument("family", type=click.Choice(sorted(FAMILIES)))
@click.option("--count", type=click.IntRange(1, len(SEEDS)), default=1, show_default=True)
@click.option(
    "--seed",
    type=click.IntRange(SEEDS.start, SEEDS.stop - 1),
    default=0,
    show_default=True,
    help="First seed.",
)
@click.option("--out", required=True, type=click.Path(path_type=Path), help="A new directory.")
def generate_cases(family: str, count: int, seed: int, out: Path) -> int:
    """Write COUNT cases of a scenario family: scenarios, operator answers, probes and an
    index. The directory appears whole or not at all, and a failure removes the parent
    directories it created."""
    if seed + count > SEEDS.stop:
        raise click.BadParameter(
            f"seeds {seed} to {seed + count - 1} pass the last seed {SEEDS.stop - 1}",
            param_hint="--count",
        )
    if out.exists() or out.is_symlink():
        raise FileExistsError(errno.EEXIST, "exists", str(out))
    try:
        cases = [generate(family, seed + offset) for offset in range(count)]
    except FamilyError as error:
        raise click.ClickException(str(error)) from error
    parent = out.absolute().parent
    created = [path for path in (parent, *parent.parents) if not path.exists()]
    staging: Path | None = None
    try:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise click.ClickException(f"cannot create {out}: {error.strerror or error}") from error
        try:
            staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=parent))
            _write_cases(staging, family, cases)
            if out.exists() or out.is_symlink():
                raise FileExistsError(errno.EEXIST, "exists", str(out))
            staging.rename(out)
        except FileExistsError:
            raise
        except OSError as error:
            raise click.ClickException(f"cannot write {out}: {error}") from error
        staging = None
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        for path in created:
            try:
                path.rmdir()
            except OSError:
                break
        raise
    click.echo(f"wrote {len(cases)} cases to {out}")
    return 0


def _write_cases(out: Path, family: str, cases: list[Case]) -> None:
    index: list[JsonValue] = []
    for case in cases:
        answers = f"{case.id}-operator.jsonl"
        _write_json(out / f"{case.id}.json", case.scenario)
        _write_private(
            out / answers,
            "".join(
                json.dumps(answer, sort_keys=True, ensure_ascii=False) + "\n"
                for answer in case.operator_answers
            ),
        )
        _write_json(
            out / f"{case.id}-probes.json",
            ProbeFile(probes=case.probes).model_dump(mode="json"),
        )
        index.append(
            {
                "id": case.id,
                "seed": case.seed,
                "scenario": f"{case.id}.json",
                "operator_answers": answers,
                "probes": f"{case.id}-probes.json",
                "facts": case.facts,
            }
        )
    _write_json(out / "family.json", {"family": family, "cases": index})


@cli.group()
def suite() -> None:
    """Run every arm on every case and compare the results."""


@suite.command("validate")
@click.argument("path", type=click.Path(path_type=Path))
def suite_validate(path: Path) -> int:
    """Resolve every arm on every case and show the planned attempts."""
    plan = plan_suite(path)
    _check_qa_files(path, plan.suite)
    click.echo(
        json.dumps(
            {
                "valid": True,
                "id": plan.suite.id,
                "attempts": len(plan.cells) * plan.suite.attempts,
                "agent_kinds": sorted({cell.agent_kind for cell in plan.cells}),
            }
        )
    )
    return 0


@suite.command("qa")
@click.argument("path", type=click.Path(path_type=Path))
@click.option(
    "--allow-command", is_flag=True, help="Run command agents as oracles; they may call a model."
)
def suite_qa(path: Path, allow_command: bool) -> int:
    """Check every case of a suite."""
    _check_qa_files(path, load_suite(path))
    reports = check_suite(path, allow_command=allow_command)
    click.echo(render_qa(reports))
    return qa_exit_code(reports)


def _suite_exit(report: SuiteReport) -> int:
    click.echo(render_report(report))
    if report.complete:
        return 0
    click.echo("incomplete: some attempts were not scored", err=True)
    return INFRASTRUCTURE_EXIT_CODE


def _stop_on_hangup() -> None:
    for number in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(number) != signal.SIG_IGN:  # nohup asks to ignore the hangup.
            signal.signal(number, signal.default_int_handler)


def _echo_attempt(arm: str, case: str, attempt: int, retry: int, outcome: str) -> None:
    label = f"attempt {attempt} retry {retry}" if retry else f"attempt {attempt}"
    click.echo(f"{arm} {case} {label}: {outcome}", err=True)


CONCURRENCY = click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="How many attempts run at once.",
)


@suite.command("run")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), required=True)
@CONCURRENCY
def suite_run(path: Path, out: Path, concurrency: int) -> int:
    """Run a suite and save every attempt plus the report in a new directory."""
    plan = plan_suite(path)
    _stop_on_hangup()
    return _suite_exit(run_suite(plan, load_json(path), out, _echo_attempt, concurrency))


@suite.command("resume")
@click.argument("out", type=click.Path(path_type=Path))
@CONCURRENCY
def suite_resume(out: Path, concurrency: int) -> int:
    """Continue a stopped suite, keeping every finished attempt."""
    _stop_on_hangup()
    return _suite_exit(resume_suite(out, _echo_attempt, concurrency))


@suite.command("report")
@click.argument("out", type=click.Path(path_type=Path))
def suite_report(out: Path) -> int:
    """Recompute a report from a suite directory."""
    return _suite_exit(read_report(out))


@cli.command("inspect")
@click.argument("source", type=click.Path(path_type=Path))
@click.option("--task", "task_id")
@click.option("--chat", "chat_id")
@click.option("--actor", "actor_id")
@click.option("--invocation", "invocation_id")
@click.option("--event", "event_id")
@click.option("--kind")
@click.option("--time-from", type=int)
@click.option("--time-to", type=int)
@click.option("--limit", type=click.IntRange(min=1))
@click.option("--verdict", "verdict_path", type=click.Path(path_type=Path))
@click.option("--provenance", "provenance_path", type=click.Path(path_type=Path))
def inspect_command(
    source: Path,
    task_id: str | None,
    chat_id: str | None,
    actor_id: str | None,
    invocation_id: str | None,
    event_id: str | None,
    kind: str | None,
    time_from: int | None,
    time_to: int | None,
    limit: int | None,
    verdict_path: Path | None,
    provenance_path: Path | None,
) -> int:
    """Inspect a saved run directory or canonical trace JSONL file."""
    trace_path, default_verdict, default_provenance = _inspection_paths(source)
    try:
        query = TraceQuery(
            task_id=task_id,
            chat_id=chat_id,
            actor_id=actor_id,
            invocation_id=invocation_id,
            event_id=event_id,
            kind=kind,
            time_from=time_from,
            time_to=time_to,
            limit=limit,
        )
        verdict = _optional_object(
            verdict_path or default_verdict, "verdict", required=verdict_path is not None
        )
        provenance = _optional_object(
            provenance_path or default_provenance,
            "provenance",
            required=provenance_path is not None,
        )
        report = inspect_trace(trace_path, query, verdict=verdict, provenance=provenance)
    except (InspectionError, ValidationError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(report.model_dump_json(indent=2))
    return 0


@cli.command()
@click.option("--runs-root", "runs_roots", multiple=True, type=click.Path(path_type=Path))
@click.option("--scenarios-root", "scenarios_roots", multiple=True, type=click.Path(path_type=Path))
@click.option("--port", type=click.IntRange(min=0, max=65535), default=4174, show_default=True)
@click.option(
    "--allow-live",
    is_flag=True,
    help="Permit starting non-scripted runs (command, persistent command, harness), which may call a live model.",
)
@click.option("--print-schema", is_flag=True, help="Print response JSON Schema and exit.")
def serve(
    runs_roots: tuple[Path, ...],
    scenarios_roots: tuple[Path, ...],
    port: int,
    allow_live: bool,
    print_schema: bool,
) -> int:
    """Serve the workbench API on 127.0.0.1."""
    if print_schema:
        click.echo(json.dumps(response_schema(), indent=2, sort_keys=True))
        return 0
    config = WorkbenchConfig(
        runs_roots=list(runs_roots) or [Path(".local/runs"), Path(".local/demos")],
        scenarios_roots=list(scenarios_roots) or [Path("examples")],
        allow_live=allow_live,
    )

    app = create_app(config)

    async def run_server() -> None:
        async with serve_http(app, port) as url:
            click.echo(json.dumps({"url": url}))
            with anyio.open_signal_receiver(signal.SIGTERM) as signals:
                await signals.__anext__()

    try:
        anyio.run(run_server)
    except KeyboardInterrupt:
        pass
    finally:
        app.state.workbench.operations.close()
    return 0


def _inspection_paths(source: Path) -> tuple[Path, Path | None, Path | None]:
    if source.is_dir():
        return source / "trace.jsonl", source / "verdict.json", source / "provenance.json"
    return source, None, None


def _optional_object(
    path: Path | None, label: str, *, required: bool
) -> dict[str, JsonValue] | None:
    if path is None:
        return None
    if not path.exists():
        if required:
            raise InspectionError(f"cannot read {label} {path}: file does not exist")
        return None
    try:
        value = load_json(path)
    except (OSError, ValueError) as error:
        raise InspectionError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise InspectionError(f"invalid {label} {path}: expected a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        return cli.main(args=argv, prog_name="overnight", standalone_mode=False)
    except click.UsageError as error:
        error.show()
        return USAGE_EXIT_CODE
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except FileExistsError as error:
        click.echo(f"Error: {error.filename} already exists", err=True)
        return USAGE_EXIT_CODE
    except (ValueError, OSError) as error:
        click.echo(f"invalid scenario: {error}", err=True)
        return 3
    except click.Abort:
        click.echo("Aborted.", err=True)
        return 1
    except Exception as error:
        InfrastructureError(error).show()
        return INFRASTRUCTURE_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
