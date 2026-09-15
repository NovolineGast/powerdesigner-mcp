"""Mutating tools: tables, columns, primary keys, references, indexes.

Every tool accepts ``dry_run``: when true, nothing is modified and the tool
returns a plan describing exactly what would happen.
"""

from __future__ import annotations

from typing import Optional

from ..errors import tool_result
from ..server_context import Backend

TOOL_DEFS = []


def register(mcp, backend: Backend) -> None:
    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    @mcp.tool(name="create_table", description=(
        "Create a table (PDM) or entity (CDM/LDM). Optionally pass an inline "
        "'columns' array (same shape as create_column's column spec) to create "
        "columns in the same call. Supports dry_run."))
    @tool_result
    def create_table(model_id: str, name: str, code: str = "", comment: str = "",
                     columns: Optional[list] = None, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            plan = [f"CREATE {'TABLE' if columns is not None else 'TABLE'} {code or name}"]
            plan += [f"  ADD COLUMN {c.get('code') or c.get('name')} "
                     f"{c.get('data_type', '')}" for c in (columns or [])]
            return {"success": True, "dry_run": True, "plan": plan,
                    "summary": {"tables": 1, "columns": len(columns or [])}}
        return {"success": True, "dry_run": False,
                "table": adapter.create_table(model_id, name, code, comment, columns)}

    @mcp.tool(name="rename_table", description=("Rename a table (Name and/or Code). Supports dry_run."))
    @tool_result
    def rename_table(model_id: str, table_ref: str, new_name: str = "",
                     new_code: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"RENAME TABLE {table_ref} -> name={new_name or '(unchanged)'} "
                             f"code={new_code or '(unchanged)'}"]}
        updates = {k: v for k, v in (("name", new_name), ("code", new_code)) if v}
        return {"success": True, "table": adapter.update_table(model_id, table_ref, updates)}

    @mcp.tool(name="update_table", description=(
        "Update table properties: name, code, comment. Supports dry_run."))
    @tool_result
    def update_table(model_id: str, table_ref: str, name: str = "", code: str = "",
                     comment: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        updates = {k: v for k, v in (("name", name), ("code", code), ("comment", comment)) if v}
        if not updates:
            return {"success": False, "error": {"code": "INVALID_PARAMS",
                                                "message": "Nothing to update"}}
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"ALTER TABLE {table_ref} SET {updates}"]}
        return {"success": True, "table": adapter.update_table(model_id, table_ref, updates)}

    @mcp.tool(name="delete_table", description=(
        "Delete a table (and PowerDesigner cascades its columns/keys/indexes and "
        "attached references). Destructive: prefer dry_run first."))
    @tool_result
    def delete_table(model_id: str, table_ref: str, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            t = adapter.get_table(model_id, table_ref)
            return {"success": True, "dry_run": True,
                    "plan": [f"DROP TABLE {t.get('code')} (columns={len(t.get('columns', []))}, "
                             f"indexes={len(t.get('indexes', []))}, refs_out="
                             f"{len(t.get('references_out', []))}, refs_in="
                             f"{len(t.get('references_in', []))})"]}
        return {"success": True, "deleted": adapter.delete_table(model_id, table_ref)}

    # ------------------------------------------------------------------
    # Column
    # ------------------------------------------------------------------
    @mcp.tool(name="create_column", description=(
        "Add a column to a table. Supported properties: name, code, data_type "
        "(e.g. 'VARCHAR', 'INT', 'DATETIME'), length, precision, mandatory, "
        "default_value, comment, description, domain (ref or code), primary "
        "(include in primary key). Supports dry_run."))
    @tool_result
    def create_column(model_id: str, table_ref: str, name: str, code: str = "",
                      data_type: str = "", length: int = 0, precision: int = 0,
                      mandatory: bool = False, default_value: str = "",
                      comment: str = "", domain: str = "", primary: bool = False,
                      dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        spec = {"name": name, "code": code or name, "data_type": data_type,
                "length": length or None, "precision": precision or None,
                "mandatory": mandatory, "default_value": default_value or None,
                "comment": comment, "domain": domain or None, "primary": primary}
        if dry_run:
            kind = f"{data_type or '?'}"
            if length:
                kind += f"({length}{',' + str(precision) if precision else ''})"
            return {"success": True, "dry_run": True,
                    "plan": [f"ALTER TABLE {table_ref} ADD COLUMN {spec['code']} {kind}"
                             f"{' NOT NULL' if mandatory else ''}"
                             f"{' PRIMARY KEY' if primary else ''}"]}
        return {"success": True, "column": adapter.create_column(model_id, table_ref, spec)}

    @mcp.tool(name="rename_column", description=("Rename a column (Name and/or Code). Supports dry_run."))
    @tool_result
    def rename_column(model_id: str, table_ref: str, column_ref: str,
                      new_name: str = "", new_code: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"RENAME COLUMN {table_ref}.{column_ref} -> "
                             f"name={new_name or '(unchanged)'} code={new_code or '(unchanged)'}"]}
        updates = {k: v for k, v in (("name", new_name), ("code", new_code)) if v}
        return {"success": True, "column": adapter.update_column(
            model_id, table_ref, column_ref, updates)}

    @mcp.tool(name="update_column", description=(
        "Update column properties: name, code, data_type, length, precision, "
        "mandatory, default_value, comment, description, domain, primary. "
        "Only supplied values are changed. Supports dry_run."))
    @tool_result
    def update_column(model_id: str, table_ref: str, column_ref: str, name: str = "",
                      code: str = "", data_type: str = "", length: int = 0,
                      precision: int = 0, mandatory: bool = False, default_value: str = "",
                      comment: str = "", domain: str = "", primary: bool = False,
                      dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        updates = {k: v for k, v in (
            ("name", name), ("code", code), ("data_type", data_type),
            ("length", length or None), ("precision", precision or None),
            ("mandatory", mandatory or None), ("default_value", default_value or None),
            ("comment", comment or None), ("domain", domain or None),
            ("primary", primary or None)) if v}
        if not updates:
            return {"success": False, "error": {"code": "INVALID_PARAMS",
                                                "message": "Nothing to update"}}
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"ALTER COLUMN {table_ref}.{column_ref} SET {updates}"]}
        return {"success": True, "column": adapter.update_column(
            model_id, table_ref, column_ref, updates)}

    @mcp.tool(name="delete_column", description=(
        "Delete a column from a table. Destructive: prefer dry_run first."))
    @tool_result
    def delete_column(model_id: str, table_ref: str, column_ref: str, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            c = adapter.get_column(model_id, table_ref, column_ref)
            return {"success": True, "dry_run": True,
                    "plan": [f"DROP COLUMN {table_ref}.{c.get('code')}"]}
        return {"success": True, "deleted": adapter.delete_column(
            model_id, table_ref, column_ref)}

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    @mcp.tool(name="create_primary_key", description=(
        "Create (or replace) the primary key of a table from the given column "
        "codes. Supports single-column and composite keys, e.g. "
        "columns=['order_id','product_id']. Existing PK membership is cleared. "
        "Supports dry_run."))
    @tool_result
    def create_primary_key(model_id: str, table_ref: str, columns: list,
                           name: str = "", code: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"PRIMARY KEY {table_ref}({', '.join(columns)})"]}
        return {"success": True, "primary_key": adapter.create_primary_key(
            model_id, table_ref, columns, name, code)}

    @mcp.tool(name="set_primary_key", description=(
        "Alias of create_primary_key: replaces the table's primary key with the "
        "given columns (composite supported)."))
    @tool_result
    def set_primary_key(model_id: str, table_ref: str, columns: list,
                        name: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"PRIMARY KEY {table_ref}({', '.join(columns)})"]}
        return {"success": True, "primary_key": adapter.create_primary_key(
            model_id, table_ref, columns, name)}

    @mcp.tool(name="remove_primary_key", description=(
        "Remove the primary key from a table. Destructive: prefer dry_run first."))
    @tool_result
    def remove_primary_key(model_id: str, table_ref: str, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"DROP PRIMARY KEY {table_ref}"]}
        return {"success": True, "removed": adapter.remove_primary_key(model_id, table_ref)}

    # ------------------------------------------------------------------
    # Reference (FK)
    # ------------------------------------------------------------------
    @mcp.tool(name="create_reference", description=(
        "Create a foreign-key reference between two PDM tables (1:N from parent "
        "to child). If parent_columns/child_columns are omitted, the parent's "
        "primary key is used and FK columns are auto-created in the child "
        "(update_key=true, e.g. user 1--N order creates order.user_id). "
        "cardinality like '0,n' or '1,1'. Supports dry_run."))
    @tool_result
    def create_reference(model_id: str, parent_table: str, child_table: str,
                         parent_columns: Optional[list] = None,
                         child_columns: Optional[list] = None, name: str = "",
                         code: str = "", comment: str = "", cardinality: str = "0,n",
                         update_key: bool = True, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            pk_note = (f"using parent PK {'+'.join(parent_columns)}" if parent_columns
                       else "using parent primary key columns")
            return {"success": True, "dry_run": True,
                    "plan": [f"REFERENCE {name or '(auto-name)'}: {parent_table} 1--N "
                             f"{child_table} ({pk_note}, cardinality={cardinality}, "
                             f"auto_fk={update_key})"]}
        return {"success": True, "reference": adapter.create_reference(
            model_id, parent_table, child_table, parent_columns, child_columns,
            name, code, comment, cardinality, update_key)}

    @mcp.tool(name="update_reference", description=(
        "Update a reference: name, code, comment, mandatory, parent_role, "
        "child_role, cardinality ('0,n', '1,1', ...). Supports dry_run."))
    @tool_result
    def update_reference(model_id: str, reference_ref: str, name: str = "", code: str = "",
                         comment: str = "", mandatory: bool = False,
                         parent_role: str = "", child_role: str = "",
                         cardinality: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        updates = {k: v for k, v in (
            ("name", name or None), ("code", code or None), ("comment", comment or None),
            ("mandatory", mandatory or None), ("parent_role", parent_role or None),
            ("child_role", child_role or None), ("cardinality", cardinality or None)) if v}
        if not updates:
            return {"success": False, "error": {"code": "INVALID_PARAMS",
                                                "message": "Nothing to update"}}
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"ALTER REFERENCE {reference_ref} SET {updates}"]}
        return {"success": True, "reference": adapter.update_reference(
            model_id, reference_ref, updates)}

    @mcp.tool(name="delete_reference", description=(
        "Delete a foreign-key reference (the FK columns in the child are kept). "
        "Destructive: prefer dry_run first."))
    @tool_result
    def delete_reference(model_id: str, reference_ref: str, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            r = adapter.get_reference(model_id, reference_ref)
            return {"success": True, "dry_run": True,
                    "plan": [f"DROP REFERENCE {r.get('code')} "
                             f"({r.get('parent_table')} -> {r.get('child_table')})"]}
        return {"success": True, "deleted": adapter.delete_reference(model_id, reference_ref)}

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------
    @mcp.tool(name="create_index", description=(
        "Create an index on a table: normal, unique or composite. Example: "
        "columns=['user_id','create_time'], name='idx_order_user_time'. "
        "Supports dry_run."))
    @tool_result
    def create_index(model_id: str, table_ref: str, columns: list, name: str = "",
                     unique: bool = False, comment: str = "", dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        idx_name = name or f"idx_{table_ref}_{'_'.join(columns)}"
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"CREATE {'UNIQUE ' if unique else ''}INDEX {idx_name} "
                             f"ON {table_ref}({', '.join(columns)})"]}
        return {"success": True, "index": adapter.create_index(
            model_id, table_ref, columns, idx_name, "", unique, comment)}

    @mcp.tool(name="update_index", description=(
        "Update an index: name, unique flag, comment, or replace its columns. "
        "Supports dry_run."))
    @tool_result
    def update_index(model_id: str, table_ref: str, index_ref: str, name: str = "",
                     unique: bool = False, comment: str = "",
                     columns: Optional[list] = None, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        updates = {k: v for k, v in (
            ("name", name or None), ("unique", unique or None),
            ("comment", comment or None), ("columns", columns)) if v}
        if not updates:
            return {"success": False, "error": {"code": "INVALID_PARAMS",
                                                "message": "Nothing to update"}}
        if dry_run:
            return {"success": True, "dry_run": True,
                    "plan": [f"ALTER INDEX {table_ref}.{index_ref} SET {updates}"]}
        return {"success": True, "index": adapter.update_index(
            model_id, table_ref, index_ref, updates)}

    @mcp.tool(name="delete_index", description=(
        "Delete an index. Destructive: prefer dry_run first."))
    @tool_result
    def delete_index(model_id: str, table_ref: str, index_ref: str, dry_run: bool = False) -> dict:
        adapter = backend.connected_adapter()
        if dry_run:
            i = adapter.get_index(model_id, table_ref, index_ref)
            return {"success": True, "dry_run": True,
                    "plan": [f"DROP INDEX {table_ref}.{i.get('code')}"]}
        return {"success": True, "deleted": adapter.delete_index(
            model_id, table_ref, index_ref)}
