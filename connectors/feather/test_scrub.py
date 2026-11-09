import anyio
from overnight_feather.native import ScrubbedShellRuntime


def test_shell_worker_environment_drops_provider_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-key")
    monkeypatch.setenv("OVERNIGHT_PROVENANCE_PATH", "/private")
    monkeypatch.setenv("VISIBLE_SETTING", "1")
    logs = tmp_path / "logs"
    logs.mkdir()

    async def exercise() -> str:
        async with ScrubbedShellRuntime(tmp_path, log_directory=logs) as runtime:
            result = await runtime.run(
                "bash",
                {
                    "command": "env",
                    "workdir": None,
                    "run_in_background": False,
                    "timeout": 30_000,
                    "description": "Print the shell environment",
                },
            )
            return str(result.content)

    output = anyio.run(exercise)
    assert "VISIBLE_SETTING=1" in output
    assert "private-key" not in output
    assert "OPENROUTER_API_KEY" not in output
    assert "OVERNIGHT_PROVENANCE_PATH" not in output
