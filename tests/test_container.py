import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from overnight.execution.container import AGENT_LABEL, OWNER_LABEL
from overnight.execution.operator import RecordedTransport
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import load_json, resolve_scenario
from overnight.execution.variations import VariationConfig, resolve_variant

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
IMAGE = "overnight-agent-base"


def _image_ready() -> bool:
    if os.environ.get("OVERNIGHT_CONTAINER_TESTS") != "1" or shutil.which("docker") is None:
        return False
    found = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    return found.returncode == 0


pytestmark = pytest.mark.skipif(
    not _image_ready(), reason=f"needs OVERNIGHT_CONTAINER_TESTS=1, Docker and {IMAGE}"
)


def _leftovers(*listing: str) -> str:
    """Containers or networks this test process started and did not remove."""
    return subprocess.run(
        [
            "docker",
            *listing,
            "--quiet",
            "--filter",
            f"label={AGENT_LABEL}=1",
            "--filter",
            f"label={OWNER_LABEL}={os.getpid()}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


SNOOP = r"""
import json, os, sys
from pathlib import Path
json.loads(sys.stdin.readline())
found = []
for root, dirs, files in os.walk("/", onerror=lambda error: None):
    if root.startswith(("/proc", "/sys", "/dev")):
        dirs[:] = []
        continue
    for name in files:
        if name in {"resolved-scenario.json", "catering-chaos.json", "scenario.json"}:
            found.append(os.path.join(root, name))
report = {
    "found": found,
    "home": os.environ.get("HOME"),
    "secret": os.environ.get("OVERNIGHT_TEST_SECRET"),
    "passed": os.environ.get("OVERNIGHT_TEST_PASSED"),
    "uid": os.getuid(),
}
print(json.dumps({"kind": "report", "message": json.dumps(report)}), flush=True)
"""


def _agent_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "agent-code"
    folder.mkdir()
    return folder


def test_oracle_runs_in_a_container_and_records_its_image(tmp_path):
    code = _agent_folder(tmp_path)
    shutil.copy(EXAMPLES / "adapters" / "episode_oracle.py", code / "oracle.py")
    shutil.copy(EXAMPLES / "shared-world-chaos-plan.json", code / "plan.json")
    document = load_json(EXAMPLES / "shared-world-chaos.json")
    document["agent"] = {
        "kind": "persistent_command",
        "command": ["python3", "/code/oracle.py", "/code/plan.json"],
        "container": {"image": IMAGE, "mounts": [{"source": str(code), "target": "/code"}]},
    }
    variation = load_json(EXAMPLES / "shared-world-chaos-variation.json")
    resolved = resolve_variant(document, None, VariationConfig.model_validate(variation))

    answers = RecordedTransport.load(EXAMPLES / "shared-world-chaos-operator.jsonl")
    result = run_scenario(resolved, operator_transport=answers)

    assert result.verdict["verdict"] == "passed"
    isolation = result.provenance["isolation"]
    assert isolation["kind"] == "container" and isolation["image"] == IMAGE
    assert str(isolation["image_id"]).startswith("sha256:")
    assert _leftovers("ps", "--all") == ""


def test_a_contained_agent_cannot_see_checks_or_the_host_environment(tmp_path, monkeypatch):
    code = _agent_folder(tmp_path)
    (code / "snoop.py").write_text(SNOOP)
    monkeypatch.setenv("OVERNIGHT_TEST_SECRET", "host-only")
    monkeypatch.setenv("OVERNIGHT_TEST_PASSED", "named")
    document = load_json(EXAMPLES / "catering-chaos.json")
    document["agent"] = {
        "kind": "command",
        "command": ["python3", "/code/snoop.py"],
        "container": {
            "image": IMAGE,
            "mounts": [{"source": str(code), "target": "/code"}],
            "env": ["OVERNIGHT_TEST_PASSED"],
        },
    }

    result = run_scenario(resolve_scenario(document))

    report = next(
        json.loads(entry["payload"]["message"])
        for entry in result.trace
        if entry["kind"] == "agent_report"
    )
    assert report["found"] == []
    assert report["home"] == "/overnight/agent"
    assert report["secret"] is None and report["passed"] == "named"
    assert report["uid"] != 0


def test_mounting_the_repository_is_refused():
    document = load_json(EXAMPLES / "catering-chaos.json")
    document["agent"] = {
        "kind": "command",
        "command": ["python3", "-c", "pass"],
        "container": {"image": IMAGE, "mounts": [{"source": str(ROOT), "target": "/repo"}]},
    }

    result = run_scenario(resolve_scenario(document))

    assert result.verdict["verdict"] == "invalid"
    assert "overlaps" in json.dumps(result.verdict["failure"])


def test_a_missing_mount_is_a_setup_error(tmp_path):
    document = load_json(EXAMPLES / "catering-chaos.json")
    document["agent"] = {
        "kind": "command",
        "command": ["python3", "-c", "pass"],
        "container": {
            "image": IMAGE,
            "mounts": [{"source": str(tmp_path / "gone"), "target": "/x"}],
        },
    }

    failure = run_scenario(resolve_scenario(document)).verdict["failure"]

    assert failure["retryable"] is False and "does not exist" in failure["message"]


PROBE = r"""
import json, os, sys, urllib.request
json.loads(sys.stdin.readline())
port = sys.argv[1]
def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status
    except Exception as error:
        return type(error).__name__
report = {
    "provider": get("https://openrouter.ai/api/v1/models"),
    "other": get("https://example.com/"),
    "host_name": get(f"http://host.docker.internal:{port}/"),
    "host_address": get(f"http://192.168.65.254:{port}/"),
}
print(json.dumps({"kind": "report", "message": json.dumps(report)}), flush=True)
"""


def _online() -> bool:
    import socket

    try:
        socket.create_connection(("openrouter.ai", 443), timeout=5).close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _online(), reason="needs internet to reach the allowed host")
def test_the_proxy_reaches_only_allowed_hosts_and_never_the_host(tmp_path):
    import http.server
    import threading

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    code = _agent_folder(tmp_path)
    (code / "probe.py").write_text(PROBE)
    document = load_json(EXAMPLES / "catering-chaos.json")
    document["agent"] = {
        "kind": "command",
        "command": ["python3", "/code/probe.py", str(server.server_address[1])],
        "container": {
            "image": IMAGE,
            "mounts": [{"source": str(code), "target": "/code"}],
            "allow_hosts": ["openrouter.ai"],
        },
    }
    try:
        result = run_scenario(resolve_scenario(document))
    finally:
        server.shutdown()

    report = next(
        json.loads(entry["payload"]["message"])
        for entry in result.trace
        if entry["kind"] == "agent_report"
    )
    assert report["provider"] == 200
    assert report["other"] != 200
    assert report["host_name"] != 200 and report["host_address"] != 200
    assert _leftovers("network", "ls") == ""


def test_a_start_that_times_out_leaves_no_proxy_or_network(tmp_path):
    code = _agent_folder(tmp_path)
    document = load_json(EXAMPLES / "catering-chaos.json")
    document["agent"] = {
        "kind": "persistent_command",
        "command": ["python3", "-c", "pass"],
        "timeout_seconds": 0.3,
        "container": {
            "image": IMAGE,
            "mounts": [{"source": str(code), "target": "/code"}],
            "allow_hosts": ["openrouter.ai"],
        },
    }

    assert run_scenario(resolve_scenario(document)).verdict["verdict"] == "invalid"

    for listing in (["ps", "--all"], ["network", "ls"]):
        assert _leftovers(*listing) == "", listing
