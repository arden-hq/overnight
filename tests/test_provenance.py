import json
import sys

import pytest
from test_runtime import BASE

from overnight.execution.adapters import CommandAdapter, ScriptedAdapter
from overnight.execution.provenance import read_connector_sidecar, read_connector_sidecars
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import resolve_scenario


def test_caller_adapter_provenance_identifies_resolved_scenario_without_claiming_a_model():
    document = json.loads(json.dumps(BASE))
    resolved = resolve_scenario(document, {"data": {"mail": {"messages": []}}})

    result = run_scenario(resolved, ScriptedAdapter([{"kind": "done"}]))

    provenance = json.loads(json.dumps(result.provenance))
    assert provenance["scenario"]["resolved"] == resolved.to_dict()
    assert provenance["scenario"]["seed"] == 0
    assert len(provenance["scenario"]["sha256"]) == 64
    assert len(provenance["backend"]["sha256"]) == 64
    assert provenance["backend"]["python"]
    assert provenance["adapter"] == {
        "identity": "overnight.execution.adapters.ScriptedAdapter",
        "kind": None,
        "owned": False,
        "code": provenance["backend"],
    }
    assert provenance["connector_status"] == "not_requested"
    assert provenance["model"] is None


def test_owned_command_reads_private_connector_sidecar(monkeypatch):
    document = json.loads(json.dumps(BASE))
    document["agent"] = {"kind": "persistent_command", "command": ["unused"]}
    sidecar = {
        "implementation": "arden",
        "code": {"git_commit": None, "sha256": "a" * 64, "version": "0.1.0"},
        "runtime": {"git_commit": None, "sha256": "b" * 64, "version": None},
        "model": {"name": "selected-model", "settings": {}, "uses_runtime_defaults": True},
    }

    class FakeAdapter(ScriptedAdapter):
        def __init__(self, path):
            super().__init__([{"kind": "done"}])
            self.path = path

        def respond(self, observation):
            self.path.write_text(json.dumps(sidecar))
            return super().respond(observation)

    monkeypatch.setattr(
        "overnight.execution.runtime.make_adapter",
        lambda config, path: FakeAdapter(path),
    )
    result = run_scenario(resolve_scenario(document))

    assert result.status == "done"
    provenance = json.loads(json.dumps(result.provenance))
    assert provenance["connector"] == sidecar
    assert provenance["connector_status"] == "available"
    assert provenance["model"] == sidecar["model"]


def test_malformed_claimed_sidecar_invalidates_run_with_evidence(monkeypatch):
    document = json.loads(json.dumps(BASE))
    document["agent"] = {"kind": "command", "command": ["unused"]}

    class FakeAdapter(ScriptedAdapter):
        def __init__(self, path):
            super().__init__([{"kind": "done"}])
            self.path = path

        def respond(self, observation):
            self.path.write_text('{"implementation":"arden"}')
            return super().respond(observation)

    monkeypatch.setattr(
        "overnight.execution.runtime.make_adapter",
        lambda config, path: FakeAdapter(path),
    )
    result = run_scenario(resolve_scenario(document))

    assert result.status == "invalid"
    assert result.verdict["verdict"] == "invalid"
    assert result.trace[-1]["kind"] == "provenance_error"
    provenance = json.loads(json.dumps(result.provenance))
    assert provenance["connector_status"] == "invalid"
    assert provenance["model"] is None


def test_one_shot_adapter_gives_each_process_a_fresh_sidecar_path(monkeypatch, tmp_path):
    paths = []

    class FakeProcess:
        image_id = None

        def __init__(self, command, timeout, path, container=None):
            paths.append(path)
            self.provenance_paths = [path]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def respond_once(self, observation):
            return {"kind": "done"}

    monkeypatch.setattr("overnight.execution.adapters.PersistentCommandAdapter", FakeProcess)
    adapter = CommandAdapter(["unused"], provenance_path=tmp_path / "connector.json")

    adapter.respond({})
    adapter.respond({})

    assert paths == [tmp_path / "connector-0.json", tmp_path / "connector-1.json"]
    assert adapter.provenance_paths == paths


def test_real_one_shot_process_sidecars_reach_provenance(tmp_path):
    sidecar = {
        "implementation": "probe",
        "code": {"git_commit": None, "sha256": "a" * 64, "version": "0.1.0"},
        "runtime": {"git_commit": None, "sha256": "b" * 64, "version": None},
        "model": {"name": "probe-model", "settings": {}, "uses_runtime_defaults": True},
    }
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, pathlib\n"
        "input()\n"
        f"pathlib.Path(os.environ['OVERNIGHT_PROVENANCE_PATH']).write_text({json.dumps(sidecar)!r})\n"
        "print(json.dumps({'kind': 'done'}))\n"
    )
    scenario = resolve_scenario(
        {**BASE, "agent": {"kind": "command", "command": [sys.executable, str(agent)]}}
    )

    provenance = run_scenario(scenario).provenance

    assert provenance["connector_status"] == "available"
    assert provenance["model"] == sidecar["model"]


def test_one_shot_sidecars_must_all_match(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps(
            {
                "implementation": "custom",
                "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
            }
        )
    )
    second.write_text(first.read_text())

    assert read_connector_sidecars([first, second]) == {
        "implementation": "custom",
        "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
        "runtime": None,
        "model": None,
    }

    second.unlink()
    with pytest.raises(ValueError, match="missing for an agent invocation"):
        read_connector_sidecars([first, second])

    second.write_text(first.read_text().replace("custom", "changed"))
    with pytest.raises(ValueError, match="changed between agent invocations"):
        read_connector_sidecars([first, second])


def test_invalid_sidecar_does_not_echo_untrusted_values(tmp_path):
    path = tmp_path / "connector.json"
    path.write_text(json.dumps({"secret-value": "another-secret"}))

    with pytest.raises(ValueError, match="validation failed") as failure:
        read_connector_sidecar(path)

    assert "secret" not in str(failure.value)


def test_backend_hash_uses_importable_package_without_checkout(monkeypatch, tmp_path):
    from overnight.execution import provenance

    package = tmp_path / "overnight"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n")
    monkeypatch.setattr(provenance, "PACKAGE", package)
    monkeypatch.setattr(provenance, "REPO", None)

    first = provenance.backend_provenance()
    (package / "module.py").write_text("value = 2\n")
    second = provenance.backend_provenance()

    assert first["git_commit"] is None
    assert first["sha256"] != second["sha256"]


def test_backend_identity_marks_dirty_checkout_and_lockfile_changes(monkeypatch, tmp_path):
    import subprocess

    from overnight.execution import provenance

    package = tmp_path / "src" / "overnight"
    package.mkdir(parents=True)
    (package / "module.py").write_text("value = 1\n")
    (tmp_path / "uv.lock").write_text("lock = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    monkeypatch.setattr(provenance, "PACKAGE", package)
    monkeypatch.setattr(provenance, "REPO", tmp_path)

    clean = provenance.backend_provenance()
    (tmp_path / "uv.lock").write_text("lock = 2\n")
    dirty = provenance.backend_provenance()

    assert dirty["git_commit"] == f"{clean['git_commit']}-dirty"
    assert dirty["sha256"] != clean["sha256"]


def test_backend_identity_is_captured_before_adapter_runs(monkeypatch):
    from overnight.execution import runtime

    identity = {"sha256": "before"}
    monkeypatch.setattr(runtime, "backend_provenance", lambda: dict(identity))

    class ChangingAdapter(ScriptedAdapter):
        def respond(self, observation):
            identity["sha256"] = "after"
            return super().respond(observation)

    result = run_scenario(
        resolve_scenario(json.loads(json.dumps(BASE))), ChangingAdapter([{"kind": "done"}])
    )

    assert json.loads(json.dumps(result.provenance))["backend"] == {"sha256": "before"}
    assert result.provenance["adapter"]["code"] is None
