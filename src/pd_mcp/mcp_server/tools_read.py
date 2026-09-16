"""Read / inspect / analysis tools."""

from __future__ import annotations

from typing import Optional

from ..errors import tool_result
from ..server_context import Backend
from ..services.inspect import compare_model as svc_compare
from ..services.inspect import inspect_schema as svc_inspect
from ..services.inspect import model_snapshot as svc_snapshot
from ..utils import paginate

TOOL_DEFS = []


def register(mcp, backend: Backend) -> None:
    @mcp.tool(name="list_tables", description=(
        "List tables (PDM) or entities (CDM/LDM) of a model. Supports text search "
        "over code/name and optional package filter. Returns brief rows - use "
        "get_table for full detail."))
    @tool_result
    def list_tables(model_id: str, query: str = "", package_ref: str = "",
                    page: int = 1, page_size: int = 50) -> dict:
        adapter = backend.connected_adapter()
        rows = adapter.list_tables(model_id, package_ref or None, query or None)
        paged = paginate(rows, page, page_size, backend.config.max_page_size)
        return {"success": True, "model_id": model_id, **paged}

    @mcp.tool(name="get_table", description=(
        "Get full detail of one table/entity by ref or code: columns (type, "
        "length, mandatory, default, comment, primary), keys, primary key, "
        "indexes and incoming/outgoing references."))
    @tool_result
    def get_table(model_id: str, table_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "table": adapter.get_table(model_id, table_ref)}

    @mcp.tool(name="search_tables", description=(
        "Search tables by substring in code or name (case-insensitive)."))
    @tool_result
    def search_tables(model_id: str, query: str, page: int = 1, page_size: int = 50) -> dict:
        adapter = backend.connected_adapter()
        rows = adapter.list_tables(model_id, None, query)
        paged = paginate(rows, page, page_size, backend.config.max_page_size)
        return {"success": True, "query": query, "model_id": model_id, **paged}

    @mcp.tool(name="list_columns", description=(
        "List the columns of a table (the attributes of a CDM/LDM entity) with "
        "full attributes. Optional text search."))
    @tool_result
    def list_columns(model_id: str, table_ref: str, query: str = "") -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "model_id": model_id, "table_ref": table_ref,
                "columns": adapter.list_columns(model_id, table_ref, query or None)}

    @mcp.tool(name="get_column", description=("Get one column's full detail."))
    @tool_result
    def get_column(model_id: str, table_ref: str, column_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True,
                "column": adapter.get_column(model_id, table_ref, column_ref)}

    @mcp.tool(name="search_columns", description=(
        "Search columns across the whole model (or one table) by substring in "
        "code/name. Returns table_code + column detail rows."))
    @tool_result
    def search_columns(model_id: str, query: str, table_ref: str = "",
                       page: int = 1, page_size: int = 100) -> dict:
        adapter = backend.connected_adapter()
        rows = []
        if table_ref:
            tables = [adapter.get_table(model_id, table_ref)]
        else:
            tables = [adapter.get_table(model_id, t["ref"])
                      for t in adapter.list_tables(model_id)]
        for t in tables:
            for c in t.get("columns", []):
                if query.lower() in (c.get("code", "") + c.get("name", "")).lower():
                    c = dict(c)
                    c["table_code"] = t.get("code")
                    rows.append(c)
        paged = paginate(rows, page, page_size, backend.config.max_page_size)
        return {"success": True, "query": query, "model_id": model_id, **paged}

    @mcp.tool(name="list_keys", description=(
        "List keys of a table. The primary key has primary=true and its columns "
        "are included (supports single-column and composite keys)."))
    @tool_result
    def list_keys(model_id: str, table_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "keys": adapter.list_keys(model_id, table_ref)}

    @mcp.tool(name="get_key", description=("Get one key by ref (from list_keys)."))
    @tool_result
    def get_key(model_id: str, table_ref: str, key_ref: str) -> dict:
        adapter = backend.connected_adapter()
        for key in adapter.list_keys(model_id, table_ref):
            if key.get("ref") == key_ref or key.get("code") == key_ref:
                return {"success": True, "key": key}
        return {"success": False, "error": {"code": "OBJECT_NOT_FOUND",
                                            "message": f"Key '{key_ref}' not found"}}

    @mcp.tool(name="list_indexes", description=(
        "List indexes of one table or of the whole model. Shows unique flag and "
        "covered columns (supports simple, unique and composite indexes)."))
    @tool_result
    def list_indexes(model_id: str, table_ref: str = "") -> dict:
        adapter = backend.connected_adapter()
        return {"success": True,
                "indexes": adapter.list_indexes(model_id, table_ref or None)}

    @mcp.tool(name="get_index", description=("Get one index's full detail."))
    @tool_result
    def get_index(model_id: str, table_ref: str, index_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "index": adapter.get_index(model_id, table_ref, index_ref)}

    @mcp.tool(name="list_references", description=(
        "List foreign-key references (PDM) of a model: parent/child tables, join "
        "column pairs, cardinality and mandatory flag."))
    @tool_result
    def list_references(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "references": adapter.list_references(model_id)}

    @mcp.tool(name="get_reference", description=("Get one reference's full detail."))
    @tool_result
    def get_reference(model_id: str, reference_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "reference": adapter.get_reference(model_id, reference_ref)}

    @mcp.tool(name="list_relationships", description=(
        "List relationships of a CDM/LDM model (entity-to-entity relations). "
        "For PDM models use list_references instead."))
    @tool_result
    def list_relationships(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "relationships": adapter.list_references(model_id)}

    @mcp.tool(name="get_relationship", description=("Get one CDM/LDM relationship by ref."))
    @tool_result
    def get_relationship(model_id: str, relationship_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True,
                "relationship": adapter.get_reference(model_id, relationship_ref)}

    @mcp.tool(name="list_domains", description=(
        "List domains (reusable data type definitions) of a model."))
    @tool_result
    def list_domains(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "domains": adapter.list_domains(model_id)}

    @mcp.tool(name="get_domain", description=("Get one domain's detail."))
    @tool_result
    def get_domain(model_id: str, domain_ref: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "domain": adapter.get_domain(model_id, domain_ref)}

    @mcp.tool(name="model_snapshot", description=(
        "Convert the model into compact JSON the AI can reason over. "
        "mode='summary': one line per table + references. mode='detail': full "
        "columns/keys/indexes/references. Optionally restrict to given table codes."))
    @tool_result
    def model_snapshot(model_id: str, mode: str = "summary") -> dict:
        adapter = backend.connected_adapter()
        snap = svc_snapshot(adapter, model_id, mode)
        snap["success"] = True
        return snap

    @mcp.tool(name="inspect_schema", description=(
        "Inspect the whole schema at once. mode='summary'|'detail', optional "
        "single table, paginated. Returns tables + foreign keys view; use this "
        "instead of many list_* calls to save context."))
    @tool_result
    def inspect_schema(model_id: str, mode: str = "summary", table: str = "",
                       page: int = 1, page_size: int = 50) -> dict:
        adapter = backend.connected_adapter()
        result = svc_inspect(adapter, model_id, mode, table or None, page, page_size,
                             backend.config.max_page_size)
        result["success"] = True
        return result

    @mcp.tool(name="compare_model", description=(
        "Structurally compare two open models: added/removed/modified tables, "
        "added/removed/modified columns per table, references and domains."))
    @tool_result
    def compare_model(model_id_a: str, model_id_b: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, "diff": svc_compare(adapter, model_id_a, model_id_b)}
