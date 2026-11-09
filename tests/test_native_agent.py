import json
import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from prepare_open_harness_suite import main  # noqa: E402


def test_suite_allows_only_the_configured_provider_host_and_key(tmp_path: Path) -> None:
    out = tmp_path / "suite.json"
    result = CliRunner().invoke(
        main,
        [
            "--base-url",
            "https://api.example.test/v1",
            "--api-key-env",
            "EXAMPLE_API_KEY",
            "--reasoning-effort",
            "none",
            "--arm",
            "pi",
            "--case",
            "capabilities",
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    agent = json.loads(out.read_text())["arms"][0]["agent"]
    assert agent["container"]["env"] == ["EXAMPLE_API_KEY"]
    assert agent["container"]["allow_hosts"] == ["api.example.test"]
    config = json.loads(agent["command"][-1])
    assert config["options"]["provider"] == {
        "base_url": "https://api.example.test/v1",
        "api_key_env": "EXAMPLE_API_KEY",
        "reasoning_effort": "none",
    }
