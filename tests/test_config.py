"""Runtime state location.

Regression guard for a packaging bug: the defaults used to derive from
``package_file.parents[2]``, which is the project root in a checkout but a
read-only directory inside an installed package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pd_mcp import config as cfg


def _checkout(root: Path) -> Path:
    (root / "src" / "pd_mcp").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return root / "src" / "pd_mcp" / "config.py"


def test_checkout_keeps_state_beside_the_project(tmp_path):
    root = tmp_path / "proj"
    pkg = _checkout(root)
    assert cfg.state_root_for(pkg, env={}) == root


def test_installed_package_uses_a_user_directory(tmp_path):
    # <base>/Lib/site-packages/pd_mcp/config.py has no pyproject.toml above it
    pkg = tmp_path / "env" / "Lib" / "site-packages" / "pd_mcp" / "config.py"
    pkg.parent.mkdir(parents=True)
    local = tmp_path / "AppData"
    assert cfg.state_root_for(pkg, env={"LOCALAPPDATA": str(local)},
                              platform="nt") == local / "powerdesigner-mcp"


def test_installed_package_on_posix_uses_xdg(tmp_path):
    pkg = tmp_path / "env" / "lib" / "python3.13" / "site-packages" / "pd_mcp" / "config.py"
    pkg.parent.mkdir(parents=True)
    state = cfg.state_root_for(pkg, env={"XDG_STATE_HOME": str(tmp_path / "state")},
                               platform="posix")
    assert state == tmp_path / "state" / "powerdesigner-mcp"


def test_env_override_wins(tmp_path):
    pkg = _checkout(tmp_path / "proj")
    override = tmp_path / "custom"
    assert cfg.state_root_for(pkg, env={"PDMCP_STATE_DIR": str(override)}) == override


def test_defaults_follow_the_state_root():
    conf = cfg.ServerConfig()
    assert conf.backup_dir == cfg.STATE_DIR / "backups"
    assert conf.log_file == cfg.STATE_DIR / "logs" / "pdmcp.log"


def test_running_from_a_checkout_keeps_the_documented_layout():
    """Dev docs and install.ps1 refer to logs/ and backups/ in the repo."""
    assert cfg.STATE_DIR == cfg.PROJECT_ROOT


@pytest.mark.parametrize("env_name,attr", [("PDMCP_BACKUP_DIR", "backup_dir"),
                                           ("PDMCP_LOG_FILE", "log_file")])
def test_env_overrides_still_win(env_name, attr, monkeypatch, tmp_path):
    target = tmp_path / "explicit"
    monkeypatch.setenv(env_name, str(target))
    conf = cfg.ServerConfig.load()
    assert Path(getattr(conf, attr)) == target
