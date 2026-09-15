# -*- coding: utf-8 -*-
"""MCP stdio smoke test: initialize -> tools/list -> tools/call round-trip."""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status} {name}" + (f": {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    vexe = PROJECT / ".venv" / "Scripts" / "python.exe"
    py = str(vexe) if vexe.exists() else sys.executable
    env = dict(os.environ)
    env["PDMCP_ADAPTER"] = "mock"
    env["PYTHONPATH"] = str(PROJECT / "src")
    env["PDMCP_LOG_LEVEL"] = "ERROR"

    proc = subprocess.Popen(
        [py, "-m", "pd_mcp", "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, cwd=str(PROJECT))

    def rpc(payload):
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line) if line else {}

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "smoke", "version": "0"}}})
        check("initialize", init.get("result", {}).get("serverInfo", {})
              .get("name") == "powerdesigner")
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        proc.stdin.flush()

        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools.get("result", {}).get("tools", [])}
        check("tools/list >= 50 tools", len(names) >= 50, f"got {len(names)}")
        for must in ("create_model", "create_table", "create_column",
                     "create_primary_key", "create_reference", "create_index",
                     "create_database_schema", "apply_schema_patch",
                     "validate_model", "check_database_design", "generate_ddl",
                     "convert_cdm_to_pdm", "begin_transaction",
                     "rollback_transaction", "inspect_schema"):
            check(f"tool {must}", must in names)

        def call(cid, name, args):
            return rpc({"jsonrpc": "2.0", "id": cid, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})

        r = call(10, "get_server_info", {})
        data = json.loads(r["result"]["content"][0]["text"])
        check("tools/call get_server_info", data.get("success") is True)
        check("backend=mock", data.get("backend") == "mock")

        r = call(11, "create_model", {"kind": "PDM", "name": "Smoke", "code": "smoke"})
        model_id = json.loads(r["result"]["content"][0]["text"])["model"]["model_id"]
        check("create_model", bool(model_id))

        r = call(12, "create_database_schema", {
            "model_id": model_id,
            "spec": {"tables": [
                {"code": "smoke_user", "name": "SmokeUser",
                 "columns": [{"code": "user_id", "data_type": "INT",
                              "primary": True},
                             {"code": "username", "data_type": "VARCHAR(50)"}]}]}})
        data = json.loads(r["result"]["content"][0]["text"])
        check("create_database_schema", data.get("success") is True)

        r = call(13, "validate_model", {"model_id": model_id})
        data = json.loads(r["result"]["content"][0]["text"])
        check("validate_model passed", data.get("passed") is True)

        out = PROJECT / "logs" / "smoke_out.sql"
        r = call(14, "generate_ddl", {"model_id": model_id,
                                      "output_path": str(out)})
        data = json.loads(r["result"]["content"][0]["text"])
        check("generate_ddl", data.get("success") and "CREATE TABLE" in data.get("sql", ""))
    finally:
        proc.kill()

    print()
    if FAILURES:
        print(f"SMOKE FAILED: {len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("SMOKE OK - all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
