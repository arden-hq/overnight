import json
from pathlib import Path

import anyio
import httpx
import pytest
from overnight_pi import RELAY_KEY_ENV, prepare
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from overnight.adapters.config import SessionContext
from overnight.adapters.http import serve_http


def _runtime(root: Path) -> Path:
    modules = root / "node_modules"
    for name, entry in (
        ("@earendil-works/pi-coding-agent", "dist/bundle/cli.js"),
        ("pi-acp", "dist/index.js"),
        ("@modelcontextprotocol/sdk", "dist/esm/index.js"),
    ):
        (modules / name / entry).parent.mkdir(parents=True)
        (modules / name / entry).touch()
        (modules / name / "package.json").write_text('{"version":"1.0.0"}')
    return root


def test_launch_points_pi_at_its_relay_which_alone_holds_the_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    async def completions(request: Request):
        seen.append(request.headers.get("authorization"))
        return JSONResponse({"id": "generation", "choices": []})

    upstream = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    monkeypatch.setenv("EXAMPLE_API_KEY", "private-test-key")
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    async def run():
        async with serve_http(upstream) as provider_url:
            context = SessionContext(
                chat_id="chat",
                directory=evidence,
                operations={},
                endpoint="http://127.0.0.1:1234",
                token="private-world-token",
                options={
                    "runtime": str(_runtime(tmp_path / "runtime")),
                    "provider": {
                        "base_url": provider_url + "/v1",
                        "api_key_env": "EXAMPLE_API_KEY",
                    },
                    "model": "example/model",
                    "input_usd_per_million": 0.2,
                    "output_usd_per_million": 0.75,
                },
            )
            launch = await prepare(context)
            assert launch.close is not None
            try:
                route = json.loads((evidence / "pi/models.json").read_text())
                route = route["providers"]["overnight"]
                assert route["apiKey"] == f"${RELAY_KEY_ENV}"
                assert not route["baseUrl"].startswith(provider_url)
                async with httpx.AsyncClient() as client:
                    sent = {"model": "example/model", "messages": []}
                    refused = await client.post(
                        route["baseUrl"] + "/chat/completions",
                        json=sent,
                        headers={"Authorization": "Bearer private-test-key"},
                    )
                    relayed = await client.post(
                        route["baseUrl"] + "/chat/completions",
                        json=sent,
                        headers={"Authorization": f"Bearer {launch.env[RELAY_KEY_ENV]}"},
                    )
            finally:
                await launch.close()
            return launch, refused, relayed

    launch, refused, relayed = anyio.run(run)

    assert (refused.status_code, relayed.status_code) == (401, 200)
    assert seen == ["Bearer private-test-key"]
    assert "private-test-key" not in json.dumps(launch.env)
    assert "private-test-key" not in (evidence / "pi/models.json").read_text()
    records = [json.loads(line) for line in (evidence / "provider.jsonl").read_text().splitlines()]
    assert records == [
        {"kind": "provider_request", "request": 1, "status": 200},
        {"kind": "provider_closed", "requests": 1},
    ]
