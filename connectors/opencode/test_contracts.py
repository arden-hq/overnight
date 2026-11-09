import hashlib
import json
from pathlib import Path
from typing import cast

import anyio
import pytest
from acp.core import ClientSideConnection
from overnight_opencode.launch import PATCHED_FILES, OpenCodeLifecycle, _patched_tree_hash, prepare
from overnight_opencode.native import (
    CHILD_UPDATE_METHOD,
    REPORT_LIMIT_BYTES,
    OpenCodeClient,
    ReportBuffer,
)

from overnight.adapters.config import SessionBinding, SessionContext
from overnight.adapters.protocol import Steer
from overnight.execution.tool_server import ToolOperation


def test_native_child_events_drive_quiescence_and_reject_wrong_roots() -> None:
    from acp.connection import StreamDirection, StreamEvent

    records = []
    client = OpenCodeClient(records.append)
    client.session_id = "root"
    observer = client.stream_observers()[0]

    def event(status: str, root: str = "root") -> StreamEvent:
        return StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "method": CHILD_UPDATE_METHOD,
                "params": {
                    "rootSessionId": root,
                    "childSessionId": "child",
                    "parentSessionId": "root",
                    "depth": 1,
                    "type": "status",
                    "status": status,
                },
            },
        )

    observer(event("running"))
    assert not client.quiescent.is_set()
    observer(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "method": CHILD_UPDATE_METHOD,
                "params": {
                    "rootSessionId": "root",
                    "childSessionId": "child",
                    "parentSessionId": "root",
                    "depth": 1,
                    "type": "update",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "actual report"},
                    },
                },
            },
        )
    )
    observer(event("completed"))
    assert client.quiescent.is_set()
    notice = client.continuation()
    assert notice is not None
    assert '"report":"actual report"' in notice
    assert '"stored_bytes":13,"total_bytes":13,"truncated":false' in notice
    assert "not a world receipt or authority" in notice
    observer(
        StreamEvent(
            StreamDirection.OUTGOING,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/prompt",
                "params": {"prompt": [{"type": "text", "text": notice}]},
            },
        )
    )
    assert client.continuation() is None
    observer(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "method": CHILD_UPDATE_METHOD,
                "params": {
                    "rootSessionId": "root",
                    "childSessionId": "child",
                    "parentSessionId": "root",
                    "depth": 1,
                    "type": "update",
                    "update": {"sessionUpdate": "tool_call_update"},
                },
            },
        )
    )
    observer(event("running"))
    assert client.children == {}
    assert client.quiescent.is_set()
    observer(event("running", root="other"))
    with pytest.raises(RuntimeError, match="invalid OpenCode child update"):
        client.raise_observer_error()


def test_native_child_report_is_bounded_across_chunk_boundaries() -> None:
    report = ReportBuffer()
    report.append("a" * (REPORT_LIMIT_BYTES - 1))
    report.append("é")
    report.append("z")

    text, metadata = report.payload()

    assert text == "a" * (REPORT_LIMIT_BYTES - 1)
    assert metadata == {
        "sha256": "644de8629cdde9481fd383aa65511bb74fa2ae49d8bcc600bc6d73112931ea6a",
        "total_bytes": REPORT_LIMIT_BYTES + 2,
        "stored_bytes": REPORT_LIMIT_BYTES - 1,
        "truncated": True,
    }


@pytest.mark.parametrize(
    ("provider", "key", "package"),
    [
        ("openrouter", "OPENROUTER_API_KEY", "@opencode/ai/providers/openrouter"),
        (
            {
                "base_url": "https://api.example.test/v1",
                "api_key_env": "EXAMPLE_API_KEY",
                "reasoning_effort": "none",
            },
            "EXAMPLE_API_KEY",
            "@opencode/ai/providers/openai-compatible-responses",
        ),
    ],
    ids=["openrouter", "compatible"],
)
def test_launch_denies_unselected_native_tools_and_uses_authenticated_world_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str | dict[str, str],
    key: str,
    package: str,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "packages/cli/src").mkdir(parents=True)
    (runtime / "packages/cli/src/index.ts").touch()
    (runtime / "packages/cli/package.json").write_text('{"version":"2.1.3"}')
    (runtime / "node_modules").mkdir()
    for relative in PATCHED_FILES:
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
    patch = Path(__file__).parent / "overnight_opencode/native-steering.patch"
    (runtime / "overnight-steering.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "version": "2.1.3",
                "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                "patched_tree_sha256": _patched_tree_hash(runtime),
            }
        )
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv(key, "private-test-key")
    launch = anyio.run(
        prepare,
        SessionContext(
            chat_id="chat",
            directory=evidence,
            operations={
                "mail": [
                    ToolOperation(
                        name="read_message",
                        description="Read one message",
                        input_schema={"type": "object"},
                    )
                ]
            },
            endpoint="http://127.0.0.1:1234",
            token="private-world-token",
            options={
                "runtime": str(runtime),
                "provider": provider,
                "native": {"allow": ["read"], "full_native_tools": False},
            },
        ),
    )

    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert config["providers"]["overnight"]["package"] == package
    assert config["providers"]["overnight"]["settings"]["apiKey"] == f"{{env:{key}}}"
    assert "private-test-key" not in launch.env["OPENCODE_CONFIG_CONTENT"]
    assert config["permissions"] == [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "mail_*", "resource": "*", "effect": "allow"},
        {"action": "read", "resource": "*", "effect": "allow"},
    ]
    assert launch.identity_source == "adapter"
    assert launch.mcp_servers == [
        {
            "type": "http",
            "name": "mail",
            "url": "http://127.0.0.1:1234/mail",
            "headers": [{"name": "Authorization", "value": "Bearer private-world-token"}],
        }
    ]
    assert launch.model is not None
    assert "private-test-key" not in launch.model.model_dump_json()
    assert launch.steer is not None


def test_steering_maps_opaque_control_target_to_bound_native_session() -> None:
    class Connection:
        def __init__(self) -> None:
            self.calls = []

        async def ext_method(self, method, params):
            self.calls.append((method, params))
            return {"outcome": "injected"}

    async def exercise() -> None:
        lifecycle = OpenCodeLifecycle()
        connection = Connection()
        lifecycle.binding = SessionBinding(
            connection=connection,
            session_id="native-session",
            client=OpenCodeClient(lambda _: None),
        )
        lifecycle.connection = cast(ClientSideConnection, connection)
        await lifecycle.steer(
            Steer(
                kind="steer",
                request_id="request",
                chat_id="chat",
                session_id="host-control-session",
                turn_id="host-control-turn",
                message="new direction",
                message_id="message",
            )
        )
        assert connection.calls == [
            (
                "session/steering",
                {
                    "sessionId": "native-session",
                    "messageId": "message",
                    "message": "new direction",
                },
            )
        ]

    anyio.run(exercise)
