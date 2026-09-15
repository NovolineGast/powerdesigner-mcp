"""Central configuration for the PowerDesigner MCP server.

Loading order (later wins):
1. Built-in defaults
2. Optional JSON config file (path from PDMCP_CONFIG env var, or ./pdmcp.json
   next to the project root when it exists)
3. PDMCP_* environment variables
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ENV_PREFIX = "PDMCP_"


def _env(name: str) -> Optional[str]:
    return os.environ.get(_ENV_PREFIX + name)


@dataclass
class ServerConfig:
    # --- PowerDesigner COM connection ---
    progid: str = "PowerDesigner.Application"
    """COM ProgID of the PowerDesigner Application object."""

    attach_mode: str = "auto"
    """auto | attach | launch | new
    auto:   attach to a running instance, else launch pdshell and attach (ROT),
            else CoCreateInstance as last resort.
    attach: only attach to a running instance (GetActiveObject).
    launch: start pdshell16.exe if not running, then attach via ROT.
    new:    CoCreateInstance (only works when COM is registered for the client
            bitness, e.g. 64-bit registration)."""

    pd_exe: Optional[str] = None
    """Full path to pdshell16.exe. Auto-detected from the registry when None."""

    visible: bool = False
    """Show the PowerDesigner window when we launch it ourselves."""

    startup_timeout_s: float = 120.0
    """Seconds to wait for a launched PowerDesigner to register in the ROT."""

    call_timeout_s: float = 300.0
    """Seconds for a single COM call before reporting a timeout error."""

    auto_quit: str = "if-launched"
    """never | if-launched | always. Quit PowerDesigner on server shutdown."""

    # --- Files / logging ---
    backup_dir: Path = PROJECT_ROOT / "backups"
    log_file: Path = PROJECT_ROOT / "logs" / "pdmcp.log"
    log_level: str = "INFO"

    # --- Tool behaviour ---
    default_page_size: int = 50
    max_page_size: int = 500

    # --- Optional model defaults ---
    default_dbms: Optional[str] = None
    """Name (or .xdb file name) of the DBMS to select when creating a PDM,
    e.g. "MySQL 5.0". None = PowerDesigner default."""

    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "ServerConfig":
        cfg = cls()

        # 2) JSON config file
        cfg_path = _env("CONFIG")
        candidates = [Path(cfg_path)] if cfg_path else [PROJECT_ROOT / "pdmcp.json"]
        for path in candidates:
            try:
                if path and path.is_file():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for key, value in data.items():
                        if hasattr(cfg, key) and not key.startswith("_"):
                            setattr(cfg, key, value)
                    break
            except Exception:
                continue

        # 3) Environment variables
        simple = {
            "PROGID": ("progid", str),
            "ATTACH_MODE": ("attach_mode", str),
            "PD_EXE": ("pd_exe", str),
            "AUTO_QUIT": ("auto_quit", str),
            "LOG_LEVEL": ("log_level", str),
            "DEFAULT_DBMS": ("default_dbms", str),
        }
        for env_name, (attr, typ) in simple.items():
            value = _env(env_name)
            if value is not None and value != "":
                setattr(cfg, attr, typ(value))

        numeric = {
            "STARTUP_TIMEOUT": "startup_timeout_s",
            "CALL_TIMEOUT": "call_timeout_s",
            "PAGE_SIZE": "default_page_size",
            "MAX_PAGE_SIZE": "max_page_size",
        }
        for env_name, attr in numeric.items():
            value = _env(env_name)
            if value is not None and value != "":
                try:
                    setattr(cfg, attr, float(value) if attr.endswith("_s") else int(value))
                except ValueError:
                    pass

        if _env("VISIBLE") is not None:
            cfg.visible = _env("VISIBLE").lower() in ("1", "true", "yes", "on")

        override_dirs = [("BACKUP_DIR", "backup_dir"), ("LOG_FILE", "log_file")]
        for env_name, attr in override_dirs:
            value = _env(env_name)
            if value:
                setattr(cfg, attr, Path(value))

        if cfg.attach_mode not in ("auto", "attach", "launch", "new"):
            cfg.attach_mode = "auto"
        if cfg.auto_quit not in ("never", "if-launched", "always"):
            cfg.auto_quit = "if-launched"
        cfg.backup_dir = Path(cfg.backup_dir)
        cfg.log_file = Path(cfg.log_file)
        return cfg

    def ensure_dirs(self) -> None:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


_config: Optional[ServerConfig] = None


def get_config() -> ServerConfig:
    global _config
    if _config is None:
        _config = ServerConfig.load()
        _config.ensure_dirs()
    return _config


def set_config(cfg: ServerConfig) -> None:
    global _config
    _config = cfg
    cfg.ensure_dirs()
