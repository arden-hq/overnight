import hashlib
import json
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
from acp.core import ClientSideConnection
from overnight_opencode.launch import (
    PATCHED_FILES,
    RELAY_KEY_ENV,
    OpenCodeLifecycle,
    _patched_tree_hash,
    prepare,
)
from overnight_opencode.native import (
    CHILD_UPDATE_METHOD,
    REPORT_LIMIT_BYTES,
    OpenCodeClient,
    ReportBuffer,
)
from overnight_opencode.options import OpenCodeOptions
from pydantic import ValidationError

from overnight.adapters.config import SessionBinding, SessionContext
from overnight.adapters.protocol import Steer
from overnight.adapters.provider_relay import ProviderRelay
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

    async def launched():
        launch = await prepare(context)
        assert launch.close is not None
        await launch.close()
        return launch

    context = SessionContext(
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
    )
    launch = anyio.run(launched)

    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    settings = config["providers"]["overnight"]["settings"]
    assert config["providers"]["overnight"]["package"] == package
    assert settings["apiKey"] == f"{{env:{RELAY_KEY_ENV}}}"
    assert settings["baseURL"].startswith("http://127.0.0.1:")
    assert key not in launch.env
    assert "private-test-key" not in json.dumps(launch.env)
    assert "private-test-key" not in (evidence / "provider.jsonl").read_text()
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


def relay_exchange(options: OpenCodeOptions, bodies: list[bytes], reply: bytes, content_type: str):
    """Send each body through a relay to a fake provider; return replies, paid calls, records."""
    paid: list[httpx.Request] = []
    records: list[dict] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        paid.append(request)
        return httpx.Response(
            200, headers={"content-type": content_type}, stream=httpx.ByteStream(reply)
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as native:
            relay = ProviderRelay(
                options,
                options.provider,
                options.upstream_model,
                "host-only-secret",
                native,
                records.append,
            )
            authorized = {"Authorization": f"Bearer {relay.token}"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=relay.app), base_url="http://relay"
            ) as client:
                return [
                    await client.post("/v1/responses", content=body, headers=authorized)
                    for body in bodies
                ]

    return anyio.run(exercise), paid, records


COMPATIBLE = {
    "base_url": "https://api.example.test/v1",
    "api_key_env": "EXAMPLE_API_KEY",
    "reasoning_effort": "none",
}
STREAM = (
    b'event: response.created\ndata: {"type":"response.created","response":{"id":"r1"}}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1",'
    b'"usage":{"input_tokens":3,"output_tokens":2}}}\n\n'
)


def test_relay_streams_responses_under_the_request_cap_and_refuses_the_next() -> None:
    options = OpenCodeOptions.model_validate(
        {"runtime": "/unused", "provider": COMPATIBLE, "max_requests": 2}
    )
    request = json.dumps(
        {"model": "openai/gpt-6-luna", "input": "hi", "stream": True, "max_output_tokens": 99999}
    ).encode()
    replies, paid, records = relay_exchange(
        options, [request, request, request], STREAM, "text/event-stream"
    )

    assert [reply.status_code for reply in replies] == [200, 200, 429]
    assert replies[0].text == STREAM.decode()
    assert len(paid) == 2
    assert paid[0].url == "https://api.example.test/v1/responses"
    assert paid[0].headers["Authorization"] == "Bearer host-only-secret"
    sent = json.loads(paid[0].content)
    assert sent["max_output_tokens"] == 2048
    assert sent["reasoning"] == {"effort": "none"}
    assert {"kind": "provider_rejected", "reason": "request_count"} in records
    assert {"kind": "provider_generation", "id": "r1"} in records
    assert "host-only-secret" not in json.dumps(records)


def test_relay_refuses_unbounded_requests_before_paying() -> None:
    options = OpenCodeOptions.model_validate({"runtime": "/unused", "max_request_bytes": 1024})
    base = {"model": "openai/gpt-6-luna", "input": "hi"}
    bodies = [
        b"x" * 1025,
        json.dumps({**base, "previous_response_id": "r0"}).encode(),
        json.dumps({**base, "tools": [{"type": "web_search"}]}).encode(),
        json.dumps(
            {**base, "input": [{"role": "user", "content": [{"type": "input_image"}]}]}
        ).encode(),
        json.dumps({**base, "input": [{"type": "item_reference", "id": "r0"}]}).encode(),
        json.dumps({**base, "model": "other"}).encode(),
    ]
    replies, paid, records = relay_exchange(options, bodies, b"{}", "application/json")

    assert [reply.status_code for reply in replies] == [413, 400, 400, 400, 400, 400]
    assert paid == []
    assert [record["reason"] for record in records] == [
        "request_bytes",
        "fields",
        "tools",
        "media",
        "stored_context",
    ]


def test_request_limits_must_fit_the_cost_cap() -> None:
    with pytest.raises(ValidationError, match="cost bound"):
        OpenCodeOptions.model_validate(
            {"runtime": "/unused", "max_requests": 40, "max_cost_usd": 1}
        )
    options = {"runtime": "/unused", "max_requests": 40, "max_cost_usd": 3}
    assert OpenCodeOptions.model_validate(options).max_requests == 40
