import asyncio

import httpx
import pytest
from overnight_prime.completion import NAMESPACE, validate_completion
from overnight_prime.options import PrimeOptions
from overnight_prime.provider import ProviderRelay

from overnight.adapters.config import NativeTurn
from overnight.adapters.evidence import UsageMeter
from overnight.execution.usage import UNMEASURED, ProcessUsage


def completion(outstanding=0, continuation_slots=0):
    return NativeTurn(
        stop_reason="end_turn",
        update_metadata=[
            {
                NAMESPACE: {
                    "phase": "responseBoundary",
                    "outcome": "result",
                    "promptTurnId": 2,
                    "eventSequence": 10,
                }
            },
            {
                NAMESPACE: {
                    "phase": "terminalQuiescence",
                    "outcome": "result",
                    "promptTurnId": 2,
                    "eventSequence": 12,
                    "quiescence": {
                        "outstandingSubagents": outstanding,
                        "remainingAutonomousContinuations": continuation_slots,
                    },
                }
            },
        ],
    )


def test_completion_requires_native_quiescence_for_the_same_turn():
    validate_completion(completion())
    validate_completion(completion(continuation_slots=2))
    with pytest.raises(RuntimeError):
        validate_completion(NativeTurn(stop_reason="end_turn"))
    with pytest.raises(RuntimeError):
        validate_completion(completion(outstanding=1))
    mismatched = completion()
    mismatched.update_metadata[1][NAMESPACE]["promptTurnId"] = 3
    with pytest.raises(RuntimeError):
        validate_completion(mismatched)


@pytest.mark.parametrize(
    ("provider", "host", "bound"),
    [
        ("openrouter", "openrouter.ai", b'"max_tokens":2048'),
        (
            {
                "base_url": "https://api.example.test/v1",
                "api_key_env": "EXAMPLE_API_KEY",
                "reasoning_effort": "none",
            },
            "api.example.test",
            b'"reasoning_effort":"none","max_completion_tokens":2048',
        ),
    ],
    ids=["openrouter", "compatible"],
)
def test_provider_rejects_forged_credentials_and_bounds_paid_requests(provider, host, bound):
    async def exercise():
        paid = []

        def upstream(request):
            paid.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        options = PrimeOptions(
            runtime="/unused",
            provider=provider,
            input_usd_per_million=0.2,
            output_usd_per_million=0.75,
            max_requests=1,
            max_request_bytes=1024,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as native:
            relay = ProviderRelay(options, "host-only-secret", native, lambda value: None)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=relay.app), base_url="http://relay"
            ) as client:
                payload = {"model": options.model, "max_tokens": 999999}
                forged = await client.post("/v1/chat/completions", json=payload)
                headers = {"Authorization": f"Bearer {relay.token}"}
                oversized = await client.post(
                    "/v1/chat/completions", content=b"x" * 1025, headers=headers
                )
                wrong_model = await client.post(
                    "/v1/chat/completions", json={"model": "other"}, headers=headers
                )
                malformed = await client.post(
                    "/v1/chat/completions", content=b"{not json", headers=headers
                )
                rerouted = await client.post(
                    "/v1/chat/completions",
                    json={**payload, "models": ["other"], "provider": {"order": ["x"]}},
                    headers=headers,
                )
                accepted = await client.post("/v1/chat/completions", json=payload, headers=headers)
                exhausted = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert [
            forged.status_code,
            oversized.status_code,
            wrong_model.status_code,
            malformed.status_code,
            rerouted.status_code,
            accepted.status_code,
            exhausted.status_code,
        ] == [401, 413, 400, 400, 400, 200, 429]
        assert len(paid) == 1
        assert paid[0].headers["Authorization"] == "Bearer host-only-secret"
        assert paid[0].url.host == host
        assert bound in paid[0].content

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/event-stream", b'data: {"id": "g"}\n\ndata: [DONE]\n\n'),
        ("application/json", b"{}"),
    ],
    ids=["stream-without-usage", "plain-response"],
)
def test_a_response_without_usage_makes_totals_unknown(tmp_path, content_type, body):
    async def exercise():
        def upstream(request):
            return httpx.Response(
                200, headers={"content-type": content_type}, stream=httpx.ByteStream(body)
            )

        options = PrimeOptions(
            runtime="/unused",
            input_usd_per_million=0.2,
            output_usd_per_million=0.75,
            max_requests=1,
            max_request_bytes=1024,
        )
        target = tmp_path / "usage.json"
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as native:
            relay = ProviderRelay(options, "key", native, lambda value: None)
            relay.meter = UsageMeter(str(target))
            relay.meter.add(3, 2, 0.5)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=relay.app), base_url="http://relay"
            ) as client:
                await client.post(
                    "/v1/chat/completions",
                    json={"model": options.model},
                    headers={"Authorization": f"Bearer {relay.token}"},
                )
        assert ProcessUsage.model_validate_json(target.read_text()) == UNMEASURED

    asyncio.run(exercise())


@pytest.mark.parametrize(("status", "cost"), [(429, 0.5), (503, None)])
def test_only_a_client_error_is_known_to_be_unbilled(tmp_path, status, cost):
    async def exercise():
        def upstream(request):
            return httpx.Response(status, json={"error": "no"})

        options = PrimeOptions(
            runtime="/unused",
            input_usd_per_million=0.2,
            output_usd_per_million=0.75,
            max_requests=1,
            max_request_bytes=1024,
        )
        target = tmp_path / "usage.json"
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as native:
            relay = ProviderRelay(options, "key", native, lambda value: None)
            relay.meter = UsageMeter(str(target))
            relay.meter.add(3, 2, 0.5)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=relay.app), base_url="http://relay"
            ) as client:
                await client.post(
                    "/v1/chat/completions",
                    json={"model": options.model},
                    headers={"Authorization": f"Bearer {relay.token}"},
                )
        assert ProcessUsage.model_validate_json(target.read_text()).cost_usd == cost

    asyncio.run(exercise())


def test_a_request_with_no_answer_makes_totals_unknown(tmp_path):
    async def exercise():
        def upstream(request):
            raise httpx.ReadTimeout("no answer", request=request)

        options = PrimeOptions(
            runtime="/unused",
            input_usd_per_million=0.2,
            output_usd_per_million=0.75,
            max_requests=1,
            max_request_bytes=1024,
        )
        target = tmp_path / "usage.json"
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as native:
            relay = ProviderRelay(options, "key", native, lambda value: None)
            relay.meter = UsageMeter(str(target))
            relay.meter.add(3, 2, 0.5)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=relay.app), base_url="http://relay"
            ) as client:
                with pytest.raises(httpx.ReadTimeout):
                    await client.post(
                        "/v1/chat/completions",
                        json={"model": options.model},
                        headers={"Authorization": f"Bearer {relay.token}"},
                    )
        assert ProcessUsage.model_validate_json(target.read_text()) == UNMEASURED

    asyncio.run(exercise())


def test_native_scheduler_deadline_and_reset_use_native_controls(tmp_path):
    from overnight_prime.lifecycle import EPOCH_MS, NativeLifecycle

    from overnight.adapters.config import SessionBinding

    async def exercise():
        clock = tmp_path / "clock"
        lifecycle = NativeLifecycle(clock)
        calls = []

        class Connection:
            active_work = False
            next_wake: int | None = EPOCH_MS + 60_000

            async def ext_method(self, method, params):
                calls.append((method, params))
                return {
                    "overnightEval": {
                        "version": 1,
                        "nowMs": int(clock.read_text()),
                        "nextWakeMs": self.next_wake,
                        "activeWork": self.active_work,
                    }
                }

            async def close_session(self, session_id):
                calls.append(("close_session", session_id))

        connection = Connection()
        await lifecycle.on_session(SessionBinding(connection, "native-session"))
        future = await lifecycle.observe({"time": 59})
        assert future.status.next_wake == 60
        assert future.request is None
        due = await lifecycle.observe({"time": 60})
        assert due.status.pending and due.status.next_wake is None
        assert due.request is not None
        assert due.request.method == "overnight/advance"
        assert due.request.params == {"sessionId": "native-session"}
        connection.active_work = True
        connection.next_wake = None
        active = await lifecycle.observe({"time": 65})
        assert active.status.pending and active.status.busy
        assert active.request is not None
        assert active.request.method == "overnight/advance"
        connection.active_work = False
        settled = await lifecycle.observe({"time": 65})
        assert not settled.status.pending and not settled.status.busy
        assert settled.request is None
        with pytest.raises(ValueError):
            await lifecycle.observe({"time": 59})
        await lifecycle.cancel()
        assert calls[-1] == ("overnight/cancel", {})
        await lifecycle.reset()
        assert calls[-2:] == [
            ("close_session", "native-session"),
            ("overnight/reset", {}),
        ]

    asyncio.run(exercise())


def test_missing_native_python_skill_is_rejected_before_startup(tmp_path):
    import sys
    from pathlib import Path

    from overnight_prime.skills import verify_python_skills

    package = tmp_path / "missing_native_skill_overnight_regression"
    package.mkdir()
    (package / "pyproject.toml").write_text('[project]\nname="missing-native-skill"\n')
    skill = package / "SKILL.md"
    skill.write_text("Native skill with an unavailable Python package")
    with pytest.raises(ValueError):
        asyncio.run(verify_python_skills(Path(sys.executable), [skill]))


def test_native_autonomy_configuration_maps_to_native_cli():
    from overnight_prime.options import AutonomousOptions

    limits = AutonomousOptions(gates=["python verify.py"], max_continuations=2, timeout_ms=120000)
    assert limits.arguments() == [
        "--autonomous-gate",
        "python verify.py",
        "--autonomous-max-continuations",
        "2",
        "--autonomous-timeout-ms",
        "120000",
    ]
    with pytest.raises(ValueError, match="requires autonomous"):
        PrimeOptions(
            runtime="/unused",
            input_usd_per_million=0.2,
            output_usd_per_million=0.75,
            autonomy=limits,
        )
    with pytest.raises(ValueError):
        AutonomousOptions(max_continuations=0)


def test_runtime_identity_covers_the_kernel_venv(tmp_path):
    from overnight_prime.launch import runtime_identity

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/prime-agent").write_text("binary")
    package = tmp_path / "kernel/lib/python3.11/site-packages/pkg-1.0.dist-info"
    package.mkdir(parents=True)
    (tmp_path / "kernel/pyvenv.cfg").write_text("home = /machine/local\nversion_info = 3.11\n")
    (package / "RECORD").write_text("pkg/__init__.py,sha256=aaa,1\n")
    before = runtime_identity(tmp_path, "0.10.0").sha256
    (package / "RECORD").write_text("pkg/__init__.py,sha256=bbb,1\n")
    assert runtime_identity(tmp_path, "0.10.0").sha256 != before
