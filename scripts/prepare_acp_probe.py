import json
from pathlib import Path

import click
from native_agent import (
    harness_agent,
    provider_options,
    runtime_and_evidence,
    save_case,
    selected_provider,
)

from overnight.adapters.provider import provider_record
from overnight.domain.contracts import JsonObject

CONTAINER_RUNTIME = "/opt/pi"
FIXTURE_REQUIRED: JsonObject = {
    "simulated_clock": False,
    "chat_isolation": True,
    "cancel": True,
    "reset_scope": "chat",
}


@click.command()
@click.option("--runtime", type=click.Path(exists=True, path_type=Path))
@click.option("--container", help="Run the agent in this image, such as overnight-pi.")
@provider_options
@click.option("--model", default="openai/gpt-6-luna", show_default=True)
@click.option("--factory", default="overnight_pi:adapter", show_default=True)
@click.option(
    "--scenario",
    "scenario_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run the agent on this scenario instead of the retained-chat probe.",
)
@click.option("--out", required=True, type=click.Path(path_type=Path))
def main(
    runtime: Path | None,
    container: str | None,
    base_url: str | None,
    api_key_env: str | None,
    reasoning_effort: str | None,
    model: str,
    factory: str,
    scenario_path: Path | None,
    out: Path,
) -> None:
    out = out.resolve()
    provider = selected_provider(base_url, api_key_env, reasoning_effort)
    runtime_path, evidence = runtime_and_evidence(runtime, container, CONTAINER_RUNTIME, out)
    config: JsonObject = {
        "factory": factory,
        "evidence": evidence,
        "options": {
            "runtime": runtime_path,
            "provider": provider_record(provider),
            "model": model,
            "input_usd_per_million": 0.2,
            "output_usd_per_million": 0.75,
        },
    }
    scenario: JsonObject = {
        "id": "acp-retained-chat",
        "seed": 47,
        "start_time": 0,
        "horizon": 20,
        "max_steps": 25,
        "actors": [
            {"id": "operator", "role": "operator"},
            {
                "id": "agent",
                "role": "agent",
                "read_services": ["mail"],
                "operations": {"mail": ["read_message", "delete_messages", "send_message"]},
            },
            {"id": "colleague", "role": "person"},
        ],
        "services": [
            {
                "id": "mail",
                "kind": "mail",
                "state": {
                    "messages": [
                        {
                            "id": "source",
                            "from": "operator",
                            "to": ["agent"],
                            "subject": "Destination",
                            "body": "The recipient actor ID is colleague.",
                            "date": 0,
                        },
                    ]
                },
            }
        ],
        "agent": harness_agent(
            config,
            out,
            container,
            provider,
            required={"simulated_clock": False},
            timeout_seconds=150,
        ),
        "tasks": [
            {
                "id": "remember",
                "chat_id": "shared",
                "message": "Remember the codeword frosted-juniper-47 for my next task. Reply ready. Do not use tools.",
                "checks": {
                    "outcomes": [{"source": "agent_report", "op": "exists", "path": ["message"]}]
                },
            },
            {
                "id": "deliver",
                "chat_id": "shared",
                "start_at": 1,
                "message": "Read message source twice using two read_message calls in one response (inside one Python call if that is your native tool). "
                "Then attempt delete_messages for source exactly once. The operator will refuse "
                "approval; do not retry or bypass that refusal. Then send one message to the "
                "recipient from source with subject Receipt and body exactly the codeword I gave "
                "you in the previous task. Do not guess it. Report what happened and finish.",
                "checks": {
                    "outcomes": [
                        {
                            "source": "state",
                            "op": "match",
                            "path": ["services", "mail", "state", "messages"],
                            "value": [
                                {
                                    "from": "agent",
                                    "subject": "Receipt",
                                    "body": "frosted-juniper-47",
                                    "draft": False,
                                }
                            ],
                        },
                    ]
                },
            },
        ],
        "operator": {
            "policy": {
                "default": "allow",
                "rules": [
                    {"service_id": "mail", "operation": "delete_messages", "mode": "approval"},
                ],
            },
            "steps": [
                {
                    "kind": "deny",
                    "at": 2,
                    "match": {
                        "service_id": "mail",
                        "operation": "delete_messages",
                        "args": {"ids": ["source"]},
                    },
                },
            ],
        },
        "checks": {
            "outcomes": [
                {
                    "source": "state",
                    "op": "match",
                    "path": ["services", "mail", "state", "messages"],
                    "value": [{"from": "agent", "to": ["colleague"], "draft": False}],
                },
            ],
            "collateral": [
                {
                    "source": "state",
                    "op": "unchanged",
                    "path": ["services", "mail", "state", "messages", 0],
                },
            ],
            "forbidden": [],
        },
    }
    if scenario_path is not None:
        given = json.loads(scenario_path.read_text())
        agent = {**scenario["agent"], "required": FIXTURE_REQUIRED, "timeout_seconds": 300}
        if "context" in given.get("agent", {}):
            agent["context"] = given["agent"]["context"]
        scenario = {**given, "agent": agent}
    click.echo(str(save_case(scenario, config, out, container)))


if __name__ == "__main__":
    main()
