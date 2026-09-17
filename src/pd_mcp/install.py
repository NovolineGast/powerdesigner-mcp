"""Self-registration with MCP clients.

Installing a locally developed MCP server used to mean hand-editing a client
config file with the absolute path of a virtual environment plus a
``PYTHONPATH`` - which is exactly the friction a published package avoids.  This
module removes it: it works out how the server should be launched in the
current environment and merges that entry into the client config itself.

Launch strategies, in order of preference:

1. ``powerdesigner-mcp`` on ``PATH`` (e.g. after ``uv tool install .``) - the
   ``npx``-style case: no interpreter path, no ``PYTHONPATH``.  The resolved
   absolute path is written so the client does not depend on inheriting ``PATH``.
2. the current interpreter running ``-m pd_mcp``, adding ``PYTHONPATH`` only
   when the package is a source checkout rather than an installed package.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SERVER_NAME = "powerdesigner"
CLIENTS = ("workbuddy", "claude-desktop", "cursor", "claude-code")

# PowerDesigner-friendly defaults; overridable from the command line
DEFAULT_ENV = {
    "PDMCP_ATTACH_MODE": "auto",
    "PDMCP_DEFAULT_DBMS": "MySQL 5.0",
}


# ---------------------------------------------------------------------------
# where each client keeps its servers
# ---------------------------------------------------------------------------

def client_config_paths(home: Optional[Path] = None,
                        appdata: Optional[Path] = None) -> Dict[str, Path]:
    home = Path(home) if home else Path.home()
    appdata = Path(appdata) if appdata else Path(
        os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    return {
        "workbuddy": home / ".workbuddy" / "mcp.json",
        "claude-desktop": appdata / "Claude" / "claude_desktop_config.json",
        "cursor": home / ".cursor" / "mcp.json",
    }


def detected_clients(home: Optional[Path] = None,
                     appdata: Optional[Path] = None) -> list[str]:
    """Clients that look installed: config present, or their dir exists."""
    found = []
    for name, path in client_config_paths(home, appdata).items():
        if path.is_file() or path.parent.is_dir():
            found.append(name)
    if shutil.which("claude"):
        found.append("claude-code")
    return found


# ---------------------------------------------------------------------------
# how the server should be launched
# ---------------------------------------------------------------------------

def _package_location() -> Optional[Path]:
    try:
        import pd_mcp
        return Path(pd_mcp.__file__).resolve()
    except Exception:
        return None


def is_installed_package() -> bool:
    """True when ``import pd_mcp`` works without help.

    Distribution metadata covers both wheel and editable installs; looking for
    "site-packages" in the module path does not, because an editable install
    keeps the source location yet still needs no ``PYTHONPATH``.
    """
    try:
        import importlib.metadata as md
        md.distribution("powerdesigner-mcp")
        return True
    except Exception:
        return False


def source_root() -> Optional[Path]:
    """The ``src`` directory of a bare source checkout, else None."""
    if is_installed_package():
        return None
    location = _package_location()
    return location.parent.parent if location else None


def uv_tool_bin_dir() -> Optional[Path]:
    """Directory uv puts tool launchers in (``uv tool dir --bin``)."""
    exe = shutil.which("uv")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "tool", "dir", "--bin"], capture_output=True,
                              text=True, timeout=30)
        if proc.returncode != 0:
            return None
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        return Path(lines[-1]) if lines else None
    except Exception:
        return None


def predicted_tool_script(name: str = "powerdesigner-mcp") -> Optional[Path]:
    """Where uv *would* put the launcher, whether or not it exists yet.

    Used to preview a registration that has not happened yet.
    """
    bin_dir = uv_tool_bin_dir()
    if bin_dir is None:
        return None
    return bin_dir / (f"{name}.exe" if os.name == "nt" else name)


def uv_tool_script(name: str = "powerdesigner-mcp") -> Optional[Path]:
    """The console script uv installed, found without relying on ``PATH``.

    MCP hosts do not always inherit the user's ``PATH``, so the tool bin
    directory is asked for explicitly rather than trusting ``shutil.which``.
    """
    bin_dir = uv_tool_bin_dir()
    if bin_dir is None:
        return None
    for candidate in (bin_dir / f"{name}.exe", bin_dir / name):
        if candidate.is_file():
            return candidate
    return None


def is_ephemeral_runtime(exe: Optional[str] = None) -> bool:
    """True when the interpreter lives in a throw-away uv/uvx cache env.

    Registering such a path produces a config that breaks as soon as the cache
    is pruned, which is exactly the kind of thing that makes a hand-written
    MCP entry rot - so it is refused instead.
    """
    path = str(exe or sys.executable).lower().replace("/", "\\")
    return ("\\uv\\cache\\" in path
            and any(marker in path for marker in
                    ("archive-v", "environments-v", "builds-v", "wheels-v")))


def persistent_install_spec() -> Optional[str]:
    """A spec that would install this package persistently.

    ``uvx``/``uv tool run`` execute in a throw-away environment, so a config
    written from there would rot.  The distribution's ``direct_url.json`` says
    where the running copy came from, which is enough to re-install it as a
    proper uv tool and register *that* launcher instead.
    """
    try:
        import importlib.metadata as md
        dist = md.distribution("powerdesigner-mcp")
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            url = str(data.get("url") or "")
            vcs = data.get("vcs_info") or {}
            if vcs.get("vcs") == "git" and url:
                commit = vcs.get("commit_id") or ""
                return f"git+{url}@{commit}" if commit else f"git+{url}"
            if url:
                return url
        # installed from an index - the bare name is enough, pinned to what is
        # running so a uvx invocation cannot silently upgrade
        return f"powerdesigner-mcp=={dist.version}"
    except Exception:
        return None


def ensure_persistent_launcher(dry_run: bool = False,
                               which=shutil.which,
                               name: str = "powerdesigner-mcp") -> Optional[Path]:
    """Install this package as a uv tool when running from a throw-away env.

    Returns the launcher path, or None when the runtime is already persistent.
    In a dry run nothing is installed, but the launcher that *would* be used is
    returned so the preview shows the real outcome instead of an error.
    """
    if not is_ephemeral_runtime():
        return None
    spec = persistent_install_spec()
    if spec is None:
        raise ValueError(
            "this interpreter lives in a temporary uv/uvx cache and the "
            "installed distribution does not record where it came from, so it "
            "cannot be re-installed persistently. Run instead:\n"
            "  uv tool install <path or git+url of this project>\n"
            "  powerdesigner-mcp install")
    if dry_run:
        return uv_tool_script(name) or predicted_tool_script(name)
    uv = which("uv") or "uv"
    cmd = [uv, "tool", "install", spec]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    launcher = uv_tool_script(name)
    if proc.returncode != 0 and launcher is None:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        hint = ""
        if not detail:
            # uv can exit silently when its own git/TLS transport is blocked
            # (seen on networks that interfere with GitHub hosts) - point at the
            # routes that do not depend on it
            hint = ("\nuv produced no error output, which usually means its "
                    "network transport could not reach the source. Alternatives:\n"
                    "  uv tool install <local path to the checkout>\n"
                    "  uvx powerdesigner-mcp install        (once released to PyPI)")
        raise ValueError(
            f"could not install the package as a uv tool ({' '.join(cmd)}):\n"
            f"{detail[:400]}{hint}")
    return launcher


def build_server_entry(name: str = DEFAULT_SERVER_NAME,
                       env: Optional[Dict[str, str]] = None,
                       extra_args: Optional[list] = None,
                       which=shutil.which,
                       tool_script: Optional[Path] = None,
                       probe_uv: bool = True,
                       command: Optional[str] = None,
                       allow_ephemeral: bool = False) -> Dict[str, Any]:
    """The ``mcpServers`` entry for the current environment.

    ``command`` overrides the whole resolution step; otherwise a console script
    on ``PATH`` wins, then uv's tool script, then this interpreter.
    """
    merged_env = dict(DEFAULT_ENV)
    merged_env.update(env or {})
    args = ["serve"] + list(extra_args or [])

    if command:
        return {"command": str(command), "args": args, "env": merged_env,
                "launch": f"explicit --command ({command})"}

    exe = which("powerdesigner-mcp") or which("powerdesigner-mcp.exe")
    launch = f"console script on PATH ({exe})"
    if not exe:
        found = tool_script if tool_script is not None else (
            uv_tool_script() if probe_uv else None)
        if found is not None:
            exe = str(found)
            launch = f"uv tool script ({exe})"
    if exe:
        return {"command": str(exe), "args": args, "env": merged_env,
                "launch": launch}

    if is_ephemeral_runtime() and not allow_ephemeral:
        raise ValueError(
            "this interpreter lives in a temporary uv/uvx cache "
            f"({sys.executable}), so a config written now would break once the "
            "cache is pruned. Install the package persistently first:\n"
            "  uv tool install --editable .\n"
            "then run: powerdesigner-mcp install\n"
            "(or pass --command <path to a permanent launcher>)")

    entry: Dict[str, Any] = {"command": sys.executable,
                             "args": ["-m", "pd_mcp", *args],
                             "env": merged_env,
                             "launch": f"interpreter module ({sys.executable})"}
    src = source_root()
    if src is not None:
        # a source checkout is not importable without help
        entry["env"]["PYTHONPATH"] = str(src)
        entry["launch"] += " + PYTHONPATH (source checkout)"
    return entry


# ---------------------------------------------------------------------------
# config merging
# ---------------------------------------------------------------------------

def merge_into_config(path: Path, name: str, entry: Dict[str, Any],
                      dry_run: bool = False, backup: bool = True) -> Dict[str, Any]:
    """Merge/replace one server entry, leaving every other key untouched."""
    payload: Dict[str, Any] = {}
    existed = path.is_file()
    if existed:
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}); refusing to "
                             "overwrite it - fix or move the file first")
        if not isinstance(payload, dict):
            raise ValueError(f"{path} does not contain a JSON object")
    servers = payload.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'mcpServers' is not an object")

    clean = {k: v for k, v in entry.items() if k != "launch"}
    previous = servers.get(name)
    # WorkBuddy keeps an enable/disable flag next to the server entry - carry it
    # over instead of silently re-enabling (or dropping) the connector
    if isinstance(previous, dict) and "disabled" in previous:
        clean["disabled"] = previous["disabled"]
    servers[name] = clean
    changed = previous != clean

    result = {"path": str(path), "existed": existed, "changed": changed,
              "written": False, "backup": None, "entry": clean,
              "replaced_existing": previous is not None}
    if dry_run or not changed:
        return result

    if backup and existed:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(path.name + f".bak-{stamp}")
        shutil.copy2(path, backup_path)
        result["backup"] = str(backup_path)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        raise OSError(f"could not write {path}: {exc}")
    result["written"] = True
    return result


def register_claude_code(name: str, entry: Dict[str, Any],
                         dry_run: bool = False,
                         which=shutil.which) -> Dict[str, Any]:
    """Claude Code is CLI-managed, so it gets a CLI command instead of a file."""
    cmd = [which("claude") or "claude", "mcp", "add", name, "--", entry["command"],
           *entry["args"]]
    for key, value in (entry.get("env") or {}).items():
        cmd += ["-e", f"{key}={value}"]
    result = {"client": "claude-code", "command": cmd, "ran": False}
    if dry_run:
        return result
    if not which("claude"):
        result["note"] = "claude CLI not on PATH; run the command manually"
        return result
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        result["ran"] = proc.returncode == 0
        result["output"] = (proc.stdout or proc.stderr or "").strip()[:500]
    except Exception as exc:
        result["note"] = f"could not run claude: {exc}"
    return result


def install(clients: Optional[list] = None, name: str = DEFAULT_SERVER_NAME,
            env: Optional[Dict[str, str]] = None, dry_run: bool = False,
            print_only: bool = False, home: Optional[Path] = None,
            appdata: Optional[Path] = None,
            which=shutil.which, probe_uv: bool = True,
            command: Optional[str] = None) -> Dict[str, Any]:
    """Register the server with the requested clients.

    ``clients`` defaults to the clients actually detected on this machine.
    Raises ``ValueError`` when the current environment cannot produce a durable
    launcher (see :func:`build_server_entry`).
    """
    # A uvx/`uv tool run` invocation is throw-away: make the install persistent
    # first, then register the launcher that will still exist tomorrow.
    promoted_launcher = ensure_persistent_launcher(dry_run=dry_run, which=which)
    entry = build_server_entry(name=name, env=env, which=which,
                               probe_uv=probe_uv, command=command,
                               tool_script=promoted_launcher)
    if print_only:
        return {"launch": entry["launch"], "server": name,
                "entry": {k: v for k, v in entry.items() if k != "launch"}}

    report: Dict[str, Any] = {"launch": entry["launch"], "clients": {},
                              "server": name, "dry_run": dry_run,
                              "persistent_launcher": (str(promoted_launcher)
                                                      if promoted_launcher else None)}

    wanted = clients or detected_clients(home, appdata) or ["workbuddy"]
    paths = client_config_paths(home, appdata)
    found_any = False
    for client in wanted:
        if client == "claude-code":
            report["clients"][client] = register_claude_code(name, entry, dry_run, which)
            continue
        path = paths.get(client)
        if path is None:
            report["clients"][client] = {"error": f"unknown client '{client}'"}
            continue
        try:
            outcome = merge_into_config(path, name, entry, dry_run=dry_run)
        except (ValueError, OSError) as exc:
            report["clients"][client] = {"error": str(exc)}
            continue
        found_any = found_any or outcome["written"]
        report["clients"][client] = outcome

    report["wrote_anything"] = found_any
    return report


def describe(report: Dict[str, Any]) -> str:
    """Human-readable summary for the CLI."""
    lines = [f"launch : {report.get('launch', '?')}"]
    if report.get("persistent_launcher"):
        lines.append(f"note   : installed persistently as a uv tool -> "
                     f"{report['persistent_launcher']}")
    if "entry" in report:
        lines.append(json.dumps({"mcpServers": {report["server"]: report["entry"]}},
                                ensure_ascii=False, indent=2))
        return "\n".join(lines)
    for client, outcome in report["clients"].items():
        if "error" in outcome:
            lines.append(f"{client:<16} ERROR {outcome['error']}")
            continue
        if client == "claude-code":
            lines.append(f"{client:<16} {'ran' if outcome.get('ran') else 'manual'}"
                         f" -> {' '.join(outcome['command'])}")
            continue
        state = ("up to date" if not outcome["changed"]
                 else ("would write" if report["dry_run"] else "written"))
        lines.append(f"{client:<16} {state}: {outcome['path']}")
        if outcome.get("backup"):
            lines.append(f"{'':<16} backup: {outcome['backup']}")
    if not report.get("wrote_anything") and not report.get("dry_run"):
        lines.append("nothing changed - the entry already matches this environment")
    return "\n".join(lines)
