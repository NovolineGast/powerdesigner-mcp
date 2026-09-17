"""Installer self-registration: launch specs and config merging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pd_mcp import install as inst


def test_entry_prefers_console_script_on_path():
    entry = inst.build_server_entry(which=lambda name: r"C:\tools\powerdesigner-mcp.exe"
                                    if name.startswith("powerdesigner-mcp") else None)
    assert entry["command"] == r"C:\tools\powerdesigner-mcp.exe"
    assert entry["args"] == ["serve"]
    assert "PATH" in entry["launch"]
    assert "PYTHONPATH" not in entry["env"]


def test_entry_uses_uv_tool_script_when_path_misses_it():
    """MCP hosts do not always inherit PATH, so uv's bin dir is probed directly."""
    entry = inst.build_server_entry(which=lambda name: None,
                                    tool_script=Path(r"D:\uv\bin\powerdesigner-mcp.exe"))
    assert entry["command"] == r"D:\uv\bin\powerdesigner-mcp.exe"
    assert "uv tool script" in entry["launch"]
    assert "PYTHONPATH" not in entry["env"]


def test_entry_falls_back_to_module_with_pythonpath(monkeypatch):
    monkeypatch.setattr(inst, "source_root", lambda: r"E:\src")
    entry = inst.build_server_entry(which=lambda name: None, probe_uv=False)
    assert entry["args"][:2] == ["-m", "pd_mcp"]
    assert entry["command"].endswith("python.exe") or "python" in entry["command"]
    assert entry["env"]["PYTHONPATH"] == r"E:\src"


def test_entry_omits_pythonpath_for_installed_package(monkeypatch):
    monkeypatch.setattr(inst, "source_root", lambda: None)
    entry = inst.build_server_entry(which=lambda name: None, probe_uv=False)
    assert "PYTHONPATH" not in entry["env"]


def test_entry_env_overrides_defaults():
    entry = inst.build_server_entry(env={"PDMCP_DEFAULT_DBMS": "Oracle 11g"},
                                   which=lambda name: "cli")
    assert entry["env"]["PDMCP_DEFAULT_DBMS"] == "Oracle 11g"
    assert entry["env"]["PDMCP_ATTACH_MODE"] == "auto"


def test_ephemeral_runtime_is_refused(monkeypatch):
    """A uvx cache interpreter would produce a config that rots - refuse it."""
    monkeypatch.setattr(inst.sys, "executable",
                        r"C:\Users\me\AppData\Local\uv\cache\archive-v0\ab\python.exe")
    monkeypatch.setattr(inst, "source_root", lambda: None)
    with pytest.raises(ValueError, match="uvx cache"):
        inst.build_server_entry(which=lambda name: None, probe_uv=False)


def test_explicit_command_bypasses_resolution(monkeypatch):
    monkeypatch.setattr(inst.sys, "executable",
                        r"C:\Users\me\AppData\Local\uv\cache\archive-v0\ab\python.exe")
    entry = inst.build_server_entry(which=lambda name: None, probe_uv=False,
                                    command=r"C:\tools\powerdesigner-mcp.exe")
    assert entry["command"] == r"C:\tools\powerdesigner-mcp.exe"
    assert "explicit" in entry["launch"]


class _FakeDist:
    version = "0.1.0"

    def __init__(self, direct_url):
        self._direct_url = direct_url

    def read_text(self, name):
        return self._direct_url


def _fake_distribution(monkeypatch, dist):
    import importlib.metadata as md

    def fake(name):
        if dist is None:
            raise md.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(md, "distribution", fake)


def test_persistent_spec_from_git_checkout(monkeypatch):
    _fake_distribution(monkeypatch, _FakeDist(json.dumps({
        "url": "https://github.com/me/proj", "vcs_info": {"vcs": "git", "commit_id": "abc123"}})))
    assert inst.persistent_install_spec() == "git+https://github.com/me/proj@abc123"


def test_persistent_spec_from_archive_url(monkeypatch):
    _fake_distribution(monkeypatch, _FakeDist(json.dumps({"url": "file:///E:/proj"})))
    assert inst.persistent_install_spec() == "file:///E:/proj"


def test_persistent_spec_pins_the_index_version(monkeypatch):
    _fake_distribution(monkeypatch, _FakeDist(None))
    assert inst.persistent_install_spec() == "powerdesigner-mcp==0.1.0"


def test_persistent_spec_unknown_distribution(monkeypatch):
    _fake_distribution(monkeypatch, None)
    assert inst.persistent_install_spec() is None


def test_ensure_launcher_is_noop_when_already_persistent(monkeypatch):
    monkeypatch.setattr(inst, "is_ephemeral_runtime", lambda exe=None: False)
    assert inst.ensure_persistent_launcher() is None


def test_ensure_launcher_dry_run_predicts_without_installing(monkeypatch):
    monkeypatch.setattr(inst, "is_ephemeral_runtime", lambda exe=None: True)
    monkeypatch.setattr(inst, "persistent_install_spec", lambda: "powerdesigner-mcp")
    monkeypatch.setattr(inst, "uv_tool_script", lambda name="powerdesigner-mcp": None)
    monkeypatch.setattr(inst, "predicted_tool_script",
                        lambda name="powerdesigner-mcp": Path(r"C:\bin\pd.exe"))
    called = []
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **k: called.append(a))
    assert inst.ensure_persistent_launcher(dry_run=True) == Path(r"C:\bin\pd.exe")
    assert called == []


def test_preview_from_a_uvx_runtime_still_shows_the_outcome(tmp_path, monkeypatch):
    """`uvx ... install --dry-run` must preview, not fail on the ephemeral env."""
    monkeypatch.setattr(inst, "is_ephemeral_runtime", lambda exe=None: True)
    monkeypatch.setattr(inst, "persistent_install_spec",
                        lambda: "powerdesigner-mcp==0.1.0")
    monkeypatch.setattr(inst, "uv_tool_script", lambda name="powerdesigner-mcp": None)
    monkeypatch.setattr(inst, "predicted_tool_script",
                        lambda name="powerdesigner-mcp": Path(r"C:\bin\pd.exe"))
    report = inst.install(print_only=True, dry_run=True, home=tmp_path,
                          which=lambda name: None, probe_uv=False)
    assert report["entry"]["command"] == r"C:\bin\pd.exe"


def test_ensure_launcher_installs_the_spec(monkeypatch):
    monkeypatch.setattr(inst, "is_ephemeral_runtime", lambda exe=None: True)
    monkeypatch.setattr(inst, "persistent_install_spec", lambda: "git+https://x/y@abc")
    monkeypatch.setattr(inst, "uv_tool_script",
                        lambda name="powerdesigner-mcp": Path(r"C:\bin\pd.exe"))
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    assert inst.ensure_persistent_launcher() == Path(r"C:\bin\pd.exe")
    assert seen["cmd"][1:] == ["tool", "install", "git+https://x/y@abc"]


def test_ensure_launcher_reports_failure(monkeypatch):
    monkeypatch.setattr(inst, "is_ephemeral_runtime", lambda exe=None: True)
    monkeypatch.setattr(inst, "persistent_install_spec", lambda: "powerdesigner-mcp")
    monkeypatch.setattr(inst, "uv_tool_script", lambda name="powerdesigner-mcp": None)
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})())
    with pytest.raises(ValueError, match="boom"):
        inst.ensure_persistent_launcher()


def test_ensure_launcher_explains_a_silent_transport_failure(monkeypatch):
    """uv exits silently when its git/TLS transport is blocked by the network."""
    monkeypatch.setattr(inst, "is_ephemeral_runtime", lambda exe=None: True)
    monkeypatch.setattr(inst, "persistent_install_spec",
                        lambda: "git+https://github.com/x/y@abc")
    monkeypatch.setattr(inst, "uv_tool_script", lambda name="powerdesigner-mcp": None)
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    with pytest.raises(ValueError) as excinfo:
        inst.ensure_persistent_launcher()
    message = str(excinfo.value)
    assert "no error output" in message
    assert "uv tool install <local path" in message


def test_install_uses_the_promoted_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "ensure_persistent_launcher",
                        lambda dry_run=False, which=inst.shutil.which: Path(r"C:\bin\pd.exe"))
    report = inst.install(clients=["workbuddy"], home=tmp_path, which=lambda name: None,
                          probe_uv=False)
    entry = json.loads((tmp_path / ".workbuddy" / "mcp.json").read_text(encoding="utf-8"))
    assert entry["mcpServers"]["powerdesigner"]["command"] == r"C:\bin\pd.exe"
    assert report["persistent_launcher"] == r"C:\bin\pd.exe"


def test_merge_creates_file_and_parents(tmp_path):
    path = tmp_path / "nested" / "mcp.json"
    entry = {"command": "x", "args": ["serve"], "env": {}}
    res = inst.merge_into_config(path, "powerdesigner", entry)
    assert res["written"] and not res["existed"] and res["backup"] is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["powerdesigner"] == entry


def test_merge_preserves_other_servers_and_keys(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "someOtherSetting": True,
        "mcpServers": {"agent-mail": {"command": "mail", "args": []}},
    }), encoding="utf-8")
    res = inst.merge_into_config(path, "powerdesigner", {"command": "x", "args": []})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert res["written"]
    assert payload["someOtherSetting"] is True
    assert payload["mcpServers"]["agent-mail"] == {"command": "mail", "args": []}
    assert payload["mcpServers"]["powerdesigner"] == {"command": "x", "args": []}
    backups = list(tmp_path.glob("mcp.json.bak-*"))
    assert len(backups) == 1 and res["backup"] == str(backups[0])


def test_merge_is_idempotent(tmp_path):
    path = tmp_path / "mcp.json"
    entry = {"command": "x", "args": ["serve"], "env": {"A": "1"}}
    assert inst.merge_into_config(path, "powerdesigner", entry)["written"]
    second = inst.merge_into_config(path, "powerdesigner", entry)
    assert second["changed"] is False and second["written"] is False
    assert second["backup"] is None


def test_merge_replaces_previous_entry_with_backup(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"powerdesigner": {"command": "old"}}}),
                    encoding="utf-8")
    res = inst.merge_into_config(path, "powerdesigner", {"command": "new", "args": []})
    assert res["changed"] and res["replaced_existing"] and res["backup"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["powerdesigner"]["command"] == "new"
    old = json.loads(Path(res["backup"]).read_text(encoding="utf-8"))
    assert old["mcpServers"]["powerdesigner"]["command"] == "old"


def test_merge_keeps_the_clients_disabled_flag(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"powerdesigner": {
        "command": "old", "disabled": False}}}), encoding="utf-8")
    inst.merge_into_config(path, "powerdesigner", {"command": "new", "args": []})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["powerdesigner"]["disabled"] is False
    assert payload["mcpServers"]["powerdesigner"]["command"] == "new"


def test_merge_does_not_invent_a_disabled_flag(tmp_path):
    path = tmp_path / "mcp.json"
    inst.merge_into_config(path, "powerdesigner", {"command": "x", "args": []})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "disabled" not in payload["mcpServers"]["powerdesigner"]


def test_merge_dry_run_touches_nothing(tmp_path):
    path = tmp_path / "mcp.json"
    res = inst.merge_into_config(path, "powerdesigner", {"command": "x"},
                                 dry_run=True)
    assert res["changed"] and not res["written"] and not path.exists()


def test_merge_refuses_invalid_json(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        inst.merge_into_config(path, "powerdesigner", {"command": "x"})
    assert path.read_text(encoding="utf-8") == "{not json"


def test_install_writes_every_requested_client(tmp_path):
    report = inst.install(clients=["workbuddy", "cursor"], home=tmp_path,
                          appdata=tmp_path / "AppData", probe_uv=False,
                          which=lambda name: r"C:\bin\powerdesigner-mcp.exe")
    assert report["wrote_anything"]
    assert set(report["clients"]) == {"workbuddy", "cursor"}
    for client in ("workbuddy", "cursor"):
        path = report["clients"][client]["path"]
        entry = json.loads(open(path, encoding="utf-8").read())["mcpServers"]["powerdesigner"]
        assert entry["command"] == r"C:\bin\powerdesigner-mcp.exe"


def test_install_print_only_returns_entry(tmp_path):
    report = inst.install(print_only=True, home=tmp_path, which=lambda name: None,
                          probe_uv=False)
    assert "entry" in report and "clients" not in report
    assert report["entry"]["args"][-1] == "serve"


def test_detected_clients_sees_existing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    (tmp_path / ".cursor").mkdir()
    assert inst.detected_clients(home=tmp_path, appdata=tmp_path / "AppData") == ["cursor"]


def test_cli_install_print_only(capsys):
    from pd_mcp.__main__ import main
    assert main(["install", "--print-only", "--name", "pd"]) == 0
    out = capsys.readouterr().out
    assert '"pd"' in out and '"serve"' in out


def test_cli_install_does_not_swallow_its_own_subcommand(capsys):
    """--command must not clobber the subcommand name argparse stores."""
    from pd_mcp.__main__ import main
    assert main(["install", "--print-only", "--command", "C:/x/pd.exe"]) == 0
    out = capsys.readouterr().out
    assert "C:/x/pd.exe" in out and "explicit" in out


def test_cli_still_accepts_legacy_commands(capsys):
    from pd_mcp.__main__ import main
    assert main(["version"]) == 0
    assert "powerdesigner-mcp" in capsys.readouterr().out
