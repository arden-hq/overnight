import asyncio
from pathlib import Path
from typing import Literal

import click

from overnight.adapters.acp import Connector
from overnight.adapters.config import AdapterConfig, load_factory
from overnight.adapters.files import write_private_json
from overnight.adapters.protocol import Observe
from overnight.domain.contracts import Contract, HarnessAgent
from overnight.execution.adapters import AdapterError, ScriptedAdapter
from overnight.execution.runtime import Trial
from overnight.execution.scenario import load_json, resolve_scenario


class DoctorReport(Contract):
    status: Literal["ready", "incompatible", "invalid"]
    model_calls: Literal[0] = 0
    session_id: str | None = None
    error: str | None = None
    scope: str = "native startup only; tool execution and task outcome are untested"


async def check(config: AdapterConfig, scenario: Path) -> DoctorReport:
    resolved = resolve_scenario(load_json(scenario))
    agent = resolved.config.agent
    if not isinstance(agent, HarnessAgent):
        raise ValueError("doctor requires a scenario with agent.kind=harness")
    factory = load_factory(config.factory)
    try:
        factory.capabilities.require(agent.required)
    except AdapterError as error:
        return DoctorReport(status="incompatible", error=str(error))
    trial = Trial(resolved, ScriptedAdapter([]))
    config = config.model_copy(
        update={"evidence": config.evidence.with_name(config.evidence.name + "-doctor")}
    )
    connector = Connector(config, factory)
    try:
        async with asyncio.timeout(config.timeout_seconds):
            await connector.start(
                Observe(kind="observe", chat_id="doctor", observation=trial.observe_agent())
            )
        return DoctorReport(status="ready", session_id=connector.client.session_id)
    finally:
        await connector.close()


@click.command()
@click.option(
    "--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--scenario", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--out", required=True, type=click.Path(path_type=Path))
def main(config_path: Path, scenario: Path, out: Path) -> None:
    """Check requirements and native startup without calling a model."""
    config = AdapterConfig.model_validate_json(config_path.read_bytes())
    try:
        result = asyncio.run(check(config, scenario))
    except Exception as error:
        result = DoctorReport(status="invalid", error=f"{type(error).__name__}: {error}")
    write_private_json(out, result.model_dump(mode="json"))
    click.echo(result.status)
    if result.status != "ready":
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    main()
