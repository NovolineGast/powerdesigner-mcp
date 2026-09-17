"""CLI entry points: serve (stdio MCP), probe (COM capability check)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# allow `python -m pd_mcp` from a source checkout
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from . import __version__  # noqa: E402
from .config import get_config  # noqa: E402
from .logging_setup import setup_logging  # noqa: E402


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="powerdesigner-mcp",
                                     description="PowerDesigner MCP Server")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the stdio MCP server (default)")
    sub.add_parser("probe", help="verify PowerDesigner COM connectivity")
    sub.add_parser("version", help="print the version")
    p_install = sub.add_parser(
        "install", help="register this server with MCP clients (no hand-edited paths)")
    p_install.add_argument("--client", action="append", dest="clients",
                           choices=["workbuddy", "claude-desktop", "cursor",
                                    "claude-code", "all"],
                           help="client to configure; repeatable "
                                "(default: every client detected on this machine)")
    p_install.add_argument("--name", default="powerdesigner",
                           help="server name in the client config")
    p_install.add_argument("--attach-mode", default="",
                           help="PDMCP_ATTACH_MODE (auto|attach|launch)")
    p_install.add_argument("--default-dbms", default="",
                           help="PDMCP_DEFAULT_DBMS, e.g. 'MySQL 5.0'")
    p_install.add_argument("--dry-run", action="store_true",
                           help="report what would change without writing")
    p_install.add_argument("--print-only", action="store_true",
                           help="print the config entry and exit")
    # dest="launcher": a plain --command would overwrite the subcommand name
    # stored in args.command by add_subparsers
    p_install.add_argument("--command", dest="launcher", default="",
                           help="launcher to register, overriding auto-detection")
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "version":
        print(f"powerdesigner-mcp {__version__}")
        return 0

    if command == "install":
        from .install import CLIENTS, describe, install
        clients = None if not args.clients else args.clients
        if clients and "all" in clients:
            clients = list(CLIENTS)
        env = {}
        if args.attach_mode:
            env["PDMCP_ATTACH_MODE"] = args.attach_mode
        if args.default_dbms:
            env["PDMCP_DEFAULT_DBMS"] = args.default_dbms
        try:
            report = install(clients=clients, name=args.name, env=env or None,
                             dry_run=args.dry_run, print_only=args.print_only,
                             command=args.launcher or None)
        except ValueError as exc:
            print(f"cannot register this environment:\n{exc}", file=sys.stderr)
            return 2
        print(describe(report))
        if not args.print_only:
            print("\nNext: open the client's connector list and Trust the new "
                  "server - it will not activate before that.")
        return 0

    config = get_config()
    setup_logging(config.log_file, config.log_level)

    if command == "serve":
        from .mcp_server.app import create_server
        mcp = create_server()
        mcp.run(transport="stdio")
        return 0

    if command == "probe":
        return _probe(config)


def _probe(config) -> int:
    """Verify PowerDesigner COM connectivity and print a capability report."""
    from .powerdesigner.com_adapter import ComAdapter
    adapter = ComAdapter(config)
    report: dict = {"steps": []}

    def step(name, fn):
        try:
            value = fn()
            report["steps"].append({"name": name, "ok": True, "result": value})
            print(f"PASS {name}: {value}", file=sys.stderr)
        except Exception as exc:
            report["steps"].append({"name": name, "ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"})
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", file=sys.stderr)

    step("connect", lambda: adapter.connect())
    step("server_info", lambda: adapter.server_info())
    step("list_models", lambda: len(adapter.list_models()))
    step("create_pdm", lambda: adapter.create_model("PDM", "MCP_Probe", "MCP_PROBE", "MySQL 5.0")["model_id"])
    mid = next((s.get("result") for s in report["steps"] if s["name"] == "create_pdm" and s["ok"]), None)
    if mid:
        step("create_table", lambda: adapter.create_table(
            mid, "ProbeUser", "probe_user")["code"])
        tref = next((s.get("result") for s in report["steps"]
                     if s["name"] == "create_table" and s["ok"]), None)
        if tref:
            step("create_column", lambda: adapter.create_column(
                mid, tref, {"name": "UserId", "code": "user_id",
                            "data_type": "INT", "mandatory": True, "primary": True})["code"])
            step("create_table2", lambda: adapter.create_table(
                mid, "ProbeOrder", "probe_order")["code"])
            tref2 = next((s.get("result") for s in report["steps"]
                          if s["name"] == "create_table2" and s["ok"]), None)
            if tref2:
                step("create_column2", lambda: adapter.create_column(
                    mid, tref2, {"name": "OrderId", "code": "order_id",
                                 "data_type": "INT", "primary": True})["code"])
                step("create_reference", lambda: adapter.create_reference(
                    mid, tref, tref2)["code"])
                step("create_index", lambda: adapter.create_index(
                    mid, tref, ["user_id"], name="idx_probe")["code"])
            step("validate", lambda: adapter.get_model_info(mid)["object_counts"])
            out = Path(config.backup_dir).parent / "logs" / "probe_output.sql"
            step("generate_database", lambda: adapter.generate_database(mid, str(out))["file"])
            step("save_as", lambda: adapter.save_model_as(
                mid, str(Path(config.backup_dir) / "probe_model.pdm"))["file"])
            step("close", lambda: adapter.close_model(mid))
    step("disconnect", lambda: adapter.disconnect() or "ok")

    report_path = Path(config.log_file).parent / "probe_report.json"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except OSError:
        pass
    ok = all(s["ok"] for s in report["steps"])
    print(json.dumps({"ok": ok, "report": str(report_path)}, ensure_ascii=False),
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
