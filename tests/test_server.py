"""Server assembly + stdio smoke tests (mock backend, no PowerDesigner)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # fall back to current interpreter
    PYTHON = Path(sys.executable)


class TestServerAssembly:
    def test_tools_registered(self):
        from pd_mcp.mcp_server.app import create_server
        mcp = create_server()
        import asyncio

        async def list_tools():
            return await mcp.list_tools()

        tools = asyncio.run(list_tools())
        names = {t.name for t in tools}
        expected = {
            "get_server_info", "list_open_models", "open_model", "create_model",
            "save_model", "save_model_as", "close_model", "get_model_info",
            "list_tables", "get_table", "search_tables", "list_columns",
            "get_column", "search_columns", "list_keys", "get_key",
            "list_indexes", "get_index", "list_references", "get_reference",
            "list_relationships", "get_relationship", "list_domains",
            "get_domain", "model_snapshot", "inspect_schema", "compare_model",
            "create_table", "rename_table", "update_table", "delete_table",
            "create_column", "rename_column", "update_column", "delete_column",
            "create_primary_key", "set_primary_key", "remove_primary_key",
            "create_reference", "update_reference", "delete_reference",
            "create_index", "update_index", "delete_index",
            "create_database_schema", "apply_schema_patch", "design_from_spec",
            "validate_model", "check_database_design", "generate_ddl",
            "convert_cdm_to_ldm", "convert_cdm_to_pdm", "convert_ldm_to_pdm",
            "begin_transaction", "commit_transaction", "rollback_transaction",
            "list_model_backups", "rollback_model",
        }
        missing = expected - names
        assert not missing, f"missing tools: {missing}"

    def test_resources_and_prompts_registered(self):
        from pd_mcp.mcp_server.app import create_server
        mcp = create_server()
        import asyncio

        async def collect():
            resources = await mcp.list_resources()
            templates = await mcp.list_resource_templates()
            prompts = await mcp.list_prompts()
            return resources, templates, prompts

        resources, templates, prompts = asyncio.run(collect())
        assert any(str(r.uri) == "powerdesigner://models" for r in resources)
        assert templates, "URI templates should be registered"
        assert any(p.name == "database_design_workflow" for p in prompts)


class TestStdioSmoke:
    def _rpc(self, proc, payload: dict) -> dict:
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, "no response from server"
        return json.loads(line)

    def test_initialize_and_call_tool(self, tmp_path):
        env = dict(os.environ)
        env["PDMCP_ADAPTER"] = "mock"
        env["PYTHONPATH"] = str(PROJECT / "src")
        env["PDMCP_LOG_LEVEL"] = "ERROR"
        proc = subprocess.Popen(
            [str(PYTHON), "-m", "pd_mcp", "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=str(PROJECT))
        try:
            init = self._rpc(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "smoke", "version": "0.0.1"}}})
            assert init["result"]["serverInfo"]["name"] == "powerdesigner"
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()

            tools = self._rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"] for t in tools["result"]["tools"]}
            assert "create_table" in names and "generate_ddl" in names

            call = self._rpc(proc, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "get_server_info", "arguments": {}}})
            text = call["result"]["content"][0]["text"]
            data = json.loads(text)
            assert data["success"] and data["backend"] == "mock"

            # full workflow through MCP: create model + table + generate DDL
            call = self._rpc(proc, {
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "create_model", "arguments": {
                    "kind": "PDM", "name": "Smoke", "code": "smoke"}}})
            model_id = json.loads(call["result"]["content"][0]["text"])["model"]["model_id"]

            call = self._rpc(proc, {
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "create_database_schema", "arguments": {
                    "model_id": model_id,
                    "spec": {"tables": [
                        {"code": "smoke_t", "name": "SmokeT",
                         "columns": [{"code": "id", "data_type": "INT",
                                      "primary": True}]}]}}}})
            assert json.loads(call["result"]["content"][0]["text"])["success"]

            ddl = tmp_path / "smoke.sql"
            call = self._rpc(proc, {
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "generate_ddl", "arguments": {
                    "model_id": model_id, "output_path": str(ddl)}}})
            assert json.loads(call["result"]["content"][0]["text"])["success"]
            assert "CREATE TABLE" in ddl.read_text(encoding="utf-8")
        finally:
            proc.kill()
