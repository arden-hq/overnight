import asyncio
import hashlib
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from acp.core import ClientSideConnection
from pydantic import BaseModel

from overnight.adapters.config import (
    Factory,
    Launch,
    NativeObservation,
    SessionBinding,
    SessionContext,
)
from overnight.adapters.evidence import source_identity
from overnight.adapters.http import serve_http
from overnight.adapters.protocol import Steer
from overnight.adapters.provider import api_key, endpoint, provider_record
from overnight.adapters.provider_relay import ProviderRelay
from overnight.execution.harness import Capabilities, HarnessStatus
from overnight.execution.provenance import ModelConfiguration
from overnight_opencode.native import OpenCodeClient
from overnight_opencode.options import OpenCodeOptions

logger = logging.getLogger(__name__)


class PackageMetadata(BaseModel):
    version: str


class SteeringBuild(BaseModel):
    protocol: int
    version: str
    patch_sha256: str
    patched_tree_sha256: str


PATCHED_FILES = [
    "packages/cli/src/acp/agent.ts",
    "packages/cli/src/acp/service.ts",
    "packages/cli/src/acp/turn.ts",
    "packages/core/src/session/run-coordinator.ts",
    "packages/core/src/session/execution.ts",
    "packages/core/src/session/session.ts",
    "packages/protocol/src/groups/session.ts",
    "packages/server/src/handlers/session.ts",
    "packages/client/src/effect/api/api.ts",
    "packages/client/src/effect/generated/client.ts",
    "packages/core/test/session-run-coordinator.test.ts",
    "packages/cli/src/util/process.ts",
]


# OpenRouter has its own OpenCode package. A configured endpoint uses the Responses route: it
# honors OpenCode's `strict: false`, so optional tool fields stay optional, where Chat Completions
# or Anthropic Messages routes may reject tool calls or make every tool field required.
OPENROUTER_PACKAGE = "@opencode/ai/providers/openrouter"
COMPATIBLE_PACKAGE = "@opencode/ai/providers/openai-compatible-responses"
# OpenCode reaches the provider only through the connector's loopback relay, which holds the key
# and bounds paid requests. OpenCode gets the relay's per-run token in this variable.
RELAY_KEY_ENV = "OVERNIGHT_OPENCODE_PROXY_KEY"


def _provider_settings(options: OpenCodeOptions) -> dict[str, str]:
    effort = endpoint(options.provider).reasoning_effort
    return {} if effort is None else {"reasoningEffort": effort}


def _patched_tree_hash(runtime: Path) -> str:
    digest = hashlib.sha256()
    for relative in sorted(PATCHED_FILES):
        digest.update(relative.encode())
        digest.update((runtime / relative).read_bytes())
    return digest.hexdigest()


class OpenCodeLifecycle:
    def __init__(self) -> None:
        self.binding: SessionBinding | None = None
        self.client: OpenCodeClient | None = None
        self.connection: ClientSideConnection | None = None

    async def bind(self, value: SessionBinding) -> None:
        if not isinstance(value.client, OpenCodeClient):
            raise TypeError("OpenCode session requires its native client")
        if not isinstance(value.connection, ClientSideConnection):
            raise TypeError("OpenCode session requires an ACP client connection")
        self.binding = value
        self.client = value.client
        self.connection = value.connection

    def bound_client(self) -> OpenCodeClient:
        if self.client is None:
            raise RuntimeError("OpenCode native client is not bound")
        return self.client

    async def observe(self, _observation) -> NativeObservation:
        client = self.bound_client()
        client.raise_observer_error()
        active = bool(client.children)
        continuation = client.continuation()
        return NativeObservation(
            status=HarnessStatus(pending=active or continuation is not None, busy=active),
            continuation=continuation,
        )

    async def after_turn(self, _turn) -> NativeObservation:
        return await self.observe({})

    async def wait_for_activity(self) -> None:
        await self.bound_client().quiescent.wait()

    async def reset(self) -> None:
        client = self.bound_client()
        client.raise_observer_error()
        if self.binding is not None and self.connection is not None:
            await self.connection.close_session(session_id=self.binding.session_id)
        client.clear_children()

    async def cancel(self) -> None:
        client = self.bound_client()
        client.raise_observer_error()
        if self.binding is None or self.connection is None:
            return
        for child_id in tuple(client.children):
            await self.connection.close_session(session_id=child_id)
        if client.children:
            async with asyncio.timeout(5):
                await client.quiescent.wait()

    async def steer(self, request: Steer) -> None:
        if self.binding is None or self.connection is None:
            raise RuntimeError("OpenCode native client is not bound")
        try:
            result = await self.connection.ext_method(
                "session/steering",
                {
                    "sessionId": self.binding.session_id,
                    "messageId": request.message_id,
                    "message": request.message,
                },
            )
        except Exception:
            logger.exception("OpenCode native steering request failed")
            raise
        if result.get("outcome") != "injected":
            raise RuntimeError("OpenCode did not inject steering into the active turn")


def _runtime_command(runtime: Path) -> tuple[list[str], Path, str]:
    runtime = runtime.resolve()
    source = runtime / "packages/cli/src/index.ts"
    package = runtime / "packages/cli/package.json"
    if source.is_file() and package.is_file():
        build_path = runtime / "overnight-steering.json"
        if not build_path.is_file():
            raise ValueError("OpenCode source runtime lacks native steering build metadata")
        build = SteeringBuild.model_validate_json(build_path.read_bytes())
        patch = Path(__file__).with_name("native-steering.patch")
        if (
            build.protocol != 1
            or build.version != "2.1.3"
            or build.patch_sha256 != hashlib.sha256(patch.read_bytes()).hexdigest()
            or build.patched_tree_sha256 != _patched_tree_hash(runtime)
        ):
            raise ValueError("OpenCode native steering build does not match connector")
        bun = shutil.which("bun")
        if bun is None:
            raise ValueError("source OpenCode runtime requires Bun")
        if not (runtime / "node_modules").is_dir():
            raise ValueError("install the pinned OpenCode runtime dependencies first")
        version = PackageMetadata.model_validate_json(package.read_bytes()).version
        if version != "2.1.3":
            raise ValueError("OpenCode adapter requires pinned source version 2.1.3")
        return (
            [bun, "run", "--jsx-import-source", "@opentui/solid", str(source), "acp"],
            runtime / "packages/cli",
            version,
        )
    raise ValueError("runtime must be the patched OpenCode 2.1.3 source build")


async def prepare(context: SessionContext) -> Launch:
    options = OpenCodeOptions.model_validate(context.options)
    command, runtime, version = _runtime_command(options.runtime)
    root = context.directory
    workspace = root / "workspace"
    config = root / "config"
    for directory in (workspace, config, root / "data", root / "cache", root / "state"):
        directory.mkdir(mode=0o700)
    secret = api_key(endpoint(options.provider))
    skills = root / "skills"
    skills.mkdir(mode=0o700)
    _link_skills(skills, options)

    def record(value):
        with (root / "provider.jsonl").open("a") as output:
            output.write(json.dumps(value) + "\n")

    os.close(os.open(root / "provider.jsonl", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    stack = AsyncExitStack()
    try:
        client = await stack.enter_async_context(httpx.AsyncClient(timeout=90))
        relay = ProviderRelay(
            options, options.provider, options.upstream_model, secret, client, record
        )
        relay_url = await stack.enter_async_context(serve_http(relay.app))

        async def close():
            record({"kind": "provider_closed", "requests": relay.requests})
            await stack.aclose()

        return _launch(context, options, (command, runtime, version), relay_url, relay.token, close)
    except BaseException:
        await stack.aclose()
        raise


def _launch(
    context: SessionContext,
    options: OpenCodeOptions,
    native: tuple[list[str], Path, str],
    relay_url: str,
    relay_token: str,
    close: Callable[[], Awaitable[None]],
) -> Launch:
    command, runtime, version = native
    root = context.directory
    workspace = root / "workspace"
    config = root / "config"
    settings = _settings(context, options, relay_url + "/v1")
    environment = {
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root / "config-home"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"),
        "OPENCODE_CONFIG_DIR": str(config),
        "OPENCODE_CONFIG_CONTENT": json.dumps(settings, separators=(",", ":")),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        RELAY_KEY_ENV: relay_token,
    }
    lifecycle = OpenCodeLifecycle()

    return Launch(
        command=command,
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
        on_session=lifecycle.bind,
        steer=lifecycle.steer,
        observe=lifecycle.observe,
        after_turn=lifecycle.after_turn,
        wait_for_activity=lifecycle.wait_for_activity,
        cancel=lifecycle.cancel,
        reset=lifecycle.reset,
        close=close,
        instructions=(
            "OpenCode starts in a private workspace and retains its native local tools. "
            "Use the supplied MCP servers for world operations. "
            "Native child sessions are available. "
            "OpenCode has no native simulated-time scheduler in this profile."
        ),
        adapter_code=source_identity(Path(__file__).parent, "0.1.0"),
        runtime_code=source_identity(runtime, f"opencode-{version}"),
        model=ModelConfiguration(
            name=options.model,
            uses_runtime_defaults=True,
            settings={
                "provider": provider_record(options.provider),
                **_provider_settings(options),
                "max_tokens": options.max_tokens,
                "max_requests": options.max_requests,
                "max_request_bytes": options.max_request_bytes,
                "max_cost_usd": options.max_cost_usd,
                "input_usd_per_million": options.input_usd_per_million,
                "output_usd_per_million": options.output_usd_per_million,
                "native": options.native.model_dump(mode="json"),
            },
        ),
    )


def _settings(context: SessionContext, options: OpenCodeOptions, base_url: str) -> dict:
    permissions = [
        {
            "action": "*",
            "resource": "*",
            "effect": "allow" if options.native.full_native_tools else "deny",
        },
        *(
            {
                "action": f"{service}_*",
                "resource": "*",
                "effect": "allow",
            }
            for service in context.operations
        ),
        *(
            {"action": action, "resource": "*", "effect": "allow"}
            for action in options.native.allow
        ),
    ]
    provider, model = options.model.split("/", 1)
    package = OPENROUTER_PACKAGE if options.provider == "openrouter" else COMPATIBLE_PACKAGE
    return {
        "model": options.model,
        "providers": {
            provider: {
                "package": package,
                "env": [RELAY_KEY_ENV],
                "settings": {
                    "baseURL": base_url,
                    "apiKey": f"{{env:{RELAY_KEY_ENV}}}",
                    **_provider_settings(options),
                },
                "models": {
                    model: {
                        "modelID": model,
                        "name": options.model,
                        "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                        "limit": {"context": 262144, "output": options.max_tokens},
                        "cost": {
                            "input": options.input_usd_per_million,
                            "output": options.output_usd_per_million,
                        },
                    }
                },
            }
        },
        "permissions": permissions,
        "default_agent": "build",
        "agents": {
            "build": {"permissions": permissions},
            "title": {"disabled": True},
            "summary": {"disabled": True},
        },
        "update": "disable",
        "share": "disabled",
        "snapshots": False,
        "experimental": {"subagent_depth": 2},
    }


def _link_skills(skills: Path, options: OpenCodeOptions) -> None:
    for skill in options.native.skills:
        source = skill.resolve()
        if not source.is_file() or source.name != "SKILL.md":
            raise ValueError(f"native skill must be an existing SKILL.md: {source}")
        target = skills / source.parent.name
        if target.exists():
            raise ValueError(f"duplicate native skill name: {source.parent.name}")
        target.symlink_to(source.parent, target_is_directory=True)


adapter = Factory(
    prepare=prepare,
    client_type=OpenCodeClient,
    capabilities=Capabilities(
        chat_isolation=True,
        cancel=True,
        reset_scope="chat",
        steering=True,
    ),
)
