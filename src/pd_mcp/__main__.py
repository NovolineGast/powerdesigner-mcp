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
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "probe", "version"])
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"powerdesigner-mcp {__version__}")
        return 0

    config = get_config()
    setup_logging(config.log_file, config.log_level)

    if args.command == "serve":
        from .mcp_server.app import create_server
        mcp = create_server()
        mcp.run(transport="stdio")
        return 0

    if args.command == "probe":
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
