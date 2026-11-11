import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from overnight.adapters.config import Factory, Launch, SessionContext
from overnight.adapters.evidence import UsageMeter, source_identity
from overnight.adapters.files import write_private_json
from overnight.adapters.http import serve_http
from overnight.adapters.provider import api_key, provider_record
from overnight.adapters.provider import endpoint as provider_endpoint
from overnight.adapters.provider_relay import ProviderRelay, run_capture
from overnight.execution.harness import Capabilities
from overnight.execution.provenance import ModelConfiguration
from overnight_prime.lifecycle import NativeLifecycle, require_evaluation_build
from overnight_prime.options import PrimeOptions
from overnight_prime.runtime import NativeRuntime, native_environment
from overnight_prime.skills import verify_python_skills


def native_skills(runtime: Path, options: PrimeOptions) -> list[Path]:
    bundled = (runtime / "bin/skills").resolve()
    skills = []
    for name in options.bundled_skills:
        skill = (bundled / name / "SKILL.md").resolve()
        if not skill.is_relative_to(bundled):
            raise ValueError("bundled skill must belong to the native runtime")
        skills.append(skill)
    skills.extend(path.resolve() for path in options.skills)
    if any(not skill.is_file() for skill in skills):
        raise ValueError("configured native skill does not exist")
    return skills


def runtime_identity(runtime: Path, release: str):
    identity = source_identity(runtime / "bin", release)
    kernel = runtime / "kernel"
    digest = hashlib.sha256(identity.sha256.encode())
    config = kernel / "pyvenv.cfg"
    if config.is_file():
        lines = sorted(
            line for line in config.read_text().splitlines() if not line.startswith("home")
        )
        digest.update("\n".join(lines).encode())
    for record in sorted(kernel.glob("lib/python*/site-packages/*.dist-info/RECORD")):
        digest.update(record.relative_to(kernel).as_posix().encode())
        digest.update(record.read_bytes())
    return identity.model_copy(update={"sha256": digest.hexdigest()})


async def prepare(context: SessionContext) -> Launch:
    options = PrimeOptions.model_validate(context.options)
    runtime = options.runtime.resolve()
    require_evaluation_build(runtime)
    skills = native_skills(runtime, options)
    binary = runtime / "bin/prime-agent"
    python = runtime / "kernel/bin/python"
    if not binary.is_file() or not python.is_file():
        raise ValueError("install and bootstrap the pinned Prime runtime first")
    await verify_python_skills(python, skills)
    version = await asyncio.create_subprocess_exec(
        str(binary),
        "--version",
        env=native_environment({}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            stdout, _ = await version.communicate()
    except BaseException:
        if version.returncode is None:
            version.kill()
        await version.wait()
        raise
    if version.returncode != 0 or stdout.decode().strip() != "0.10.0":
        raise ValueError("Prime adapter requires native runtime 0.10.0")
    root = context.directory
    workspace = root / "workspace"
    agent = root / "prime"
    workspace.mkdir(mode=0o700)
    agent.mkdir(mode=0o700)
    stack = AsyncExitStack()
    temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="onp-", dir="/tmp")))
    daemon_socket = temporary / "daemon.sock"

    def record(value):
        with (root / "prime.jsonl").open("a") as output:
            output.write(json.dumps(value) + "\n")

    descriptor = os.open(root / "prime.jsonl", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        client = await stack.enter_async_context(httpx.AsyncClient(timeout=90))
        relay = ProviderRelay(
            options,
            options.provider,
            options.model,
            api_key(provider_endpoint(options.provider)),
            client,
            record,
            UsageMeter(),
            capture=run_capture(),
        )
        endpoint = await stack.enter_async_context(serve_http(relay.app))
        write_private_json(
            agent / "models.json",
            {
                "providers": {
                    "overnight": {
                        "baseUrl": endpoint + "/v1",
                        "api": "openai-completions",
                        "apiKey": "OVERNIGHT_PRIME_PROXY_KEY",
                        "models": [
                            {
                                "id": options.model,
                                "reasoning": False,
                                "input": ["text"],
                                "maxTokens": options.max_tokens,
                            }
                        ],
                    }
                },
            },
        )
        write_private_json(
            agent / "settings.json",
            {
                "mcpServers": {
                    service: {
                        "type": "http",
                        "url": f"{context.endpoint}/{service}",
                        "headers": {"Authorization": f"Bearer {context.token}"},
                    }
                    for service in context.operations
                }
            },
        )
        lifecycle = NativeLifecycle(root / "native-clock")
        environment = {
            "PRIME_AGENT_EVAL_CLOCK": str(lifecycle.clock),
            "HOME": str(root),
            "TMPDIR": str(temporary),
            "PRIME_AGENT_CODING_AGENT_DIR": str(agent),
            "PRIME_AGENT_KERNEL_PYTHON": str(python),
            "PRIME_AGENT_TELEMETRY": "0",
            "PRIME_AGENT_DAEMON_SOCKET": str(daemon_socket),
            "OVERNIGHT_PRIME_PROXY_KEY": relay.token,
        }

        native = NativeRuntime(binary, workspace, native_environment(environment), root, record)
        await native.start(daemon_socket)
        stack.push_async_callback(native.close)

        async def close():
            record({"kind": "provider_closed", "requests": relay.requests})
            await stack.aclose()

        return Launch(
            meters_usage=True,
            command=[
                str(binary),
                "--mode",
                "acp",
                "--provider",
                "overnight",
                "--model",
                options.model,
                "--daemon-socket",
                str(daemon_socket),
                "--offline",
                "--no-skills",
                "--no-context-files",
                "--append-system-prompt",
                "Use the pre-imported mcp helper or from rlm import mcp. Do not import mcp (the SDK). "
                "Discover service operations with await mcp.list_tools(server), then call "
                "await mcp.call_tool(server, operation, arguments). Connected servers: "
                + ", ".join(context.operations),
            ]
            + [argument for skill in skills for argument in ("--skill", str(skill))]
            + (["--autonomous", *options.autonomy.arguments()] if options.autonomous else []),
            instructions=(
                "Native rlm.spawn starts its child task only after your current turn ends. "
                "After spawning, finish your turn so the child can start; do not collect or "
                "send follow-ups within that same turn. Native quiescence waits for the child. "
                "Use native rlm_heartbeat for scheduled work. Its clock follows world time. "
                "Native schedules stay pending after you answer; recurring jobs run until deleted. "
                "Use native Python and pathlib for files within your private working directory; "
                "the interpreter is available independently of world MCP tools. "
                "Use the supplied world tools for service I/O. Native subagents may use these tools."
            ),
            on_session=lifecycle.on_session,
            observe=lifecycle.observe,
            after_turn=lifecycle.after_turn,
            reset=lifecycle.reset,
            cancel=lifecycle.cancel,
            cwd=workspace,
            env=environment,
            mcp_servers=[
                {
                    "type": "http",
                    "name": service,
                    "url": f"{context.endpoint}/{service}",
                    "headers": [{"name": "Authorization", "value": f"Bearer {context.token}"}],
                }
                for service in context.operations
            ],
            identity_source="adapter",
            close=close,
            adapter_code=source_identity(Path(__file__).parent, "0.1.0"),
            runtime_code=runtime_identity(runtime, "0.10.0"),
            model=ModelConfiguration(
                name=options.model,
                uses_runtime_defaults=True,
                settings={
                    "provider": provider_record(options.provider),
                    "max_tokens": options.max_tokens,
                    "max_requests": options.max_requests,
                    "max_request_bytes": options.max_request_bytes,
                    "max_cost_usd": options.max_cost_usd,
                    "native_tool": "python_repl",
                    "native_eval_protocol": 1,
                    "autonomous": options.autonomous,
                    "autonomy": options.autonomy.model_dump(mode="json"),
                    "skills": [str(skill) for skill in skills],
                },
            ),
        )
    except BaseException:
        await stack.aclose()
        raise


adapter = Factory(
    capabilities=Capabilities(
        simulated_clock=True, chat_isolation=True, cancel=True, reset_scope="chat"
    ),
    prepare=prepare,
)
