"""Model management + system tools."""

from __future__ import annotations

from typing import Optional

from ..errors import tool_result
from ..server_context import Backend

TOOL_DEFS = []


def register(mcp, backend: Backend) -> None:
    @mcp.tool(name="get_server_info", description=(
        "Check the PowerDesigner connection and report server capabilities. "
        "Call this first to verify PowerDesigner is reachable (or that the "
        "mock backend is active). Returns version, attach mode, open model count."))
    @tool_result
    def get_server_info() -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, **adapter.server_info(),
                "backend": "mock" if adapter.__class__.__name__ == "MockAdapter" else "com"}

    @mcp.tool(name="list_open_models", description=(
        "List all models currently open in PowerDesigner with kind (PDM/CDM/LDM), "
        "name, code, file path, DBMS and object counts."))
    @tool_result
    def list_open_models() -> dict:
        adapter = backend.connected_adapter()
        models = adapter.list_models()
        return {"success": True, "models": models, "count": len(models)}

    @mcp.tool(name="open_model", description=(
        "Open a PowerDesigner model file (.pdm / .cdm / .ldm) and return its info. "
        "Set read_only=true to prevent modifications."))
    @tool_result
    def open_model(path: str, read_only: bool = False) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "model": adapter.open_model(path, read_only)}

    @mcp.tool(name="create_model", description=(
        "Create a new empty model. kind: 'PDM' | 'CDM' | 'LDM'. For PDM you may "
        "pass dbms (e.g. 'MySQL 5.0'); PowerDesigner picks its default when omitted. "
        "Returns the new model info including its model_id."))
    @tool_result
    def create_model(kind: str, name: str, code: str = "", dbms: str = "") -> dict:
        adapter = backend.connected_adapter()
        return {"success": True,
                "model": adapter.create_model(kind, name, code, dbms or None)}

    @mcp.tool(name="save_model", description=(
        "Save a model to its current file. Fails with a clear error when the model "
        "was never saved (use save_model_as first)."))
    @tool_result
    def save_model(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, **adapter.save_model(model_id)}

    @mcp.tool(name="save_model_as", description=(
        "Save a model to a new file path (.pdm/.cdm/.ldm extension is appended "
        "automatically). The target file must not exist yet."))
    @tool_result
    def save_model_as(model_id: str, path: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, **adapter.save_model_as(model_id, path)}

    @mcp.tool(name="close_model", description=(
        "Close a model in PowerDesigner. save=true saves to its file before closing."))
    @tool_result
    def close_model(model_id: str, save: bool = False) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, **adapter.close_model(model_id, save)}

    @mcp.tool(name="get_model_info", description=(
        "Get detailed info about one open model: kind, name, code, file, DBMS, "
        "and counts of tables/entities, columns, keys, references, indexes, domains."))
    @tool_result
    def get_model_info(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "model": adapter.get_model_info(model_id)}

    @mcp.tool(name="list_packages", description=(
        "List packages (sub-models/namespaces) of a model."))
    @tool_result
    def list_packages(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "packages": adapter.list_packages(model_id)}

    @mcp.tool(name="get_package", description=(
        "Get details of one package by ref, code or name."))
    @tool_result
    def get_package(model_id: str, package_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "package": adapter.get_package(model_id, package_ref)}
