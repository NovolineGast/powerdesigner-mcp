"""High-level schema automation.

* :func:`create_database_schema` - create a whole schema (tables, columns,
  PKs, references, indexes, domains) in one call.
* :func:`apply_schema_patch` - apply an ordered list of structured operations.
* :func:`design_from_spec` - materialise a structured design spec into a new
  (or existing) model of any kind (PDM / CDM / LDM).

All three share the same execution core: they build a flat, ordered operation
plan which supports ``dry_run`` (return the plan without touching the model)
and ``atomic`` (execute inside a transaction; any failure rolls the model
back to its pre-operation state when the model is file-backed).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import InvalidParamsError, ObjectNotFoundError, OperationFailedError

# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def _column_ops(table_code: str, columns: List[Dict[str, Any]], pk_columns: List[str]) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    for col in columns or []:
        code = col.get("code") or col.get("name")
        if not code:
            raise InvalidParamsError(f"Column in table '{table_code}' is missing 'code'/'name'")
        spec = {k: col.get(k) for k in ("name", "code", "data_type", "length", "precision",
                                        "mandatory", "default_value", "comment", "description",
                                        "domain")}
        spec["code"] = code
        spec["primary"] = bool(col.get("primary")) or (code in pk_columns)
        ops.append({"op": "add_column", "table": table_code, "column": spec})
    return ops


def build_schema_ops(spec: Dict[str, Any], model_kind: str = "PDM") -> List[Dict[str, Any]]:
    """Turn a create_database_schema / design_from_spec payload into flat ops."""
    if not isinstance(spec, dict):
        raise InvalidParamsError("schema spec must be a JSON object")
    ops: List[Dict[str, Any]] = []

    if model_kind != "PDM" and (spec.get("indexes")
                               or any(e.get("indexes") for e in spec.get("tables") or [])):
        raise InvalidParamsError(
            f"Indexes do not exist in a {model_kind} model - they are a physical "
            "(PDM) concept. Drop the index entries and convert to a PDM first.")

    for dom in spec.get("domains") or []:
        ops.append({"op": "create_domain", "domain": dom})

    entity_word = "table" if model_kind == "PDM" else "entity"
    for entry in spec.get("tables") or []:
        code = entry.get("code") or entry.get("name")
        if not code:
            raise InvalidParamsError(f"A {entity_word} entry is missing 'code'/'name'")
        pk_columns: List[str] = list(entry.get("primary_key_columns") or [])
        for col in entry.get("columns") or []:
            if col.get("primary") and (col.get("code") or col.get("name")) not in pk_columns:
                pk_columns.append(col.get("code") or col.get("name"))
        create: Dict[str, Any] = {
            "op": "create_table", "table": {
                "name": entry.get("name") or code, "code": code,
                "comment": entry.get("comment", ""),
            }
        }
        ops.append(create)
        ops.extend(_column_ops(code, entry.get("columns") or [], pk_columns))
        if pk_columns:
            ops.append({"op": "create_primary_key", "table": code, "columns": pk_columns,
                        "name": entry.get("primary_key_name") or
                                (f"PK_{code}" if model_kind == "PDM" else f"ID_{code}")})
        # inline indexes
        for idx in entry.get("indexes") or []:
            ops.append({"op": "create_index", "table": code,
                        "index": {"name": idx.get("name"), "code": idx.get("code"),
                                  "columns": idx.get("columns") or [],
                                  "unique": bool(idx.get("unique")),
                                  "comment": idx.get("comment", "")}})

    for idx in spec.get("indexes") or []:
        ops.append({"op": "create_index", "table": idx.get("table"),
                    "index": {"name": idx.get("name"), "code": idx.get("code"),
                              "columns": idx.get("columns") or [],
                              "unique": bool(idx.get("unique")),
                              "comment": idx.get("comment", "")}})

    for rel in spec.get("relationships") or []:
        ops.append({"op": "create_reference", "reference": {
            "parent_table": rel.get("parent_table") or rel.get("parent"),
            "child_table": rel.get("child_table") or rel.get("child"),
            "parent_columns": rel.get("parent_columns"),
            "child_columns": rel.get("child_columns"),
            "name": rel.get("name"), "code": rel.get("code"),
            "comment": rel.get("comment", ""),
            "cardinality": rel.get("cardinality"),
            "parent_cardinality": rel.get("parent_cardinality"),
            "dependent_role": rel.get("dependent_role"),
        }})
    return ops


def _summary(ops: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for o in ops:
        counts[o["op"]] = counts.get(o["op"], 0) + 1
    return counts


def _model_kind(adapter, model_id: str) -> str:
    """Kind of an existing model, for vocabulary-correct plan rendering."""
    try:
        return (adapter.get_model_info(model_id).get("kind") or "PDM").upper()
    except Exception:
        return "PDM"


# ---------------------------------------------------------------------------
# Patch operations
# ---------------------------------------------------------------------------

PATCH_OPS = {
    "create_table", "alter_table", "rename_table", "drop_table",
    "add_column", "alter_column", "drop_column",
    "create_primary_key", "drop_primary_key",
    "create_reference", "drop_reference", "alter_reference",
    "create_index", "drop_index", "alter_index", "create_domain",
}


def _describe(op: Dict[str, Any], model_kind: str = "PDM") -> str:
    """Human-readable one-liner for an op, in the target model's vocabulary."""
    conceptual = model_kind.upper() != "PDM"
    entity = "ENTITY" if conceptual else "TABLE"
    member = "ATTRIBUTE" if conceptual else "COLUMN"
    key_word = "IDENTIFIER" if conceptual else "PRIMARY KEY"
    o = op.get("op")
    if o == "create_table":
        return f"CREATE {entity} {op['table']['code']}"
    if o == "add_column":
        return f"ADD {member} {op['table']}.{op['column']['code']}"
    if o == "create_primary_key":
        return f"{key_word} {op['table']}({', '.join(op['columns'])})"
    if o == "create_reference":
        ref = op["reference"]
        card = ref.get("cardinality") or "0,n"
        parent_card = ref.get("parent_cardinality") or "1,1"
        dep = f" dependent_role={ref['dependent_role']}" if ref.get("dependent_role") else ""
        label = "RELATIONSHIP" if conceptual else "REFERENCE"
        return (f"{label} {ref.get('name') or ''}: {ref['parent_table']} "
                f"({card}) -> {ref['child_table']} ({parent_card}){dep}")
    if o == "create_index":
        idx = op["index"]
        return f"INDEX {op['table']}.({', '.join(idx.get('columns') or [])})"
    if o == "create_domain":
        d = op["domain"]
        return f"DOMAIN {d.get('code') or d.get('name')}"
    if o == "drop_table":
        return f"DROP {entity} {op['table']}"
    if o == "drop_column":
        return f"DROP {member} {op['table']}.{op['column']}"
    if o == "drop_reference":
        return f"DROP REFERENCE {op['reference']}"
    if o == "drop_index":
        return f"DROP INDEX {op['table']}.{op['index']}"
    if o == "alter_table":
        return f"ALTER {entity} {op['table']} {op.get('updates')}"
    if o == "rename_table":
        return f"RENAME {entity} {op['table']} -> {op.get('new_name')}/{op.get('new_code')}"
    if o == "alter_column":
        return f"ALTER {member} {op['table']}.{op['column']} {op.get('updates')}"
    if o == "drop_primary_key":
        return f"DROP {key_word} {op['table']}"
    if o == "alter_reference":
        return f"ALTER REFERENCE {op['reference']} {op.get('updates')}"
    if o == "alter_index":
        return f"ALTER INDEX {op['table']}.{op['index']} {op.get('updates')}"
    return o or "?"


def _capture_old(adapter, model_id: str, op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Snapshot current props of the object an UPDATE op is about to change."""
    name = op.get("op")
    try:
        if name in ("alter_table", "rename_table"):
            return adapter.get_object_props(model_id, "table", op["table"])
        if name == "alter_reference":
            return adapter.get_object_props(model_id, "reference", op["reference"])
    except Exception:
        return None
    return None


_CREATE_KIND = {
    "create_table": "table",
    "add_column": "column",
    "create_reference": "reference",
    "create_index": "index",
    "create_domain": "domain",
    "create_primary_key": "key",
}


def _record(txn_manager, txn_id: str, model_id: str, op: Dict[str, Any],
            result: Any, old_props: Optional[Dict[str, Any]]) -> None:
    """Journal an executed operation so transactions can undo it."""
    name = op.get("op")
    try:
        kind = _CREATE_KIND.get(name)
        if kind and isinstance(result, dict) and result.get("ref"):
            txn_manager.record(txn_id, {"type": "create_object", "model_id": model_id,
                                        "kind": kind, "obj_ref": result["ref"],
                                        "code": result.get("code", "")})
        elif name in ("alter_table", "rename_table") and old_props:
            txn_manager.record(txn_id, {"type": "update_props", "model_id": model_id,
                                        "kind": "table", "obj_ref": op["table"],
                                        "old": old_props})
        elif name == "alter_reference" and old_props:
            txn_manager.record(txn_id, {"type": "update_props", "model_id": model_id,
                                        "kind": "reference", "obj_ref": op["reference"],
                                        "old": old_props})
        elif name in ("drop_table", "drop_column", "drop_reference", "drop_index"):
            txn_manager.record(txn_id, {"type": "delete_object", "model_id": model_id,
                                        "kind": name.replace("drop_", ""),
                                        "obj_ref": op.get("table") or op.get("reference")
                                        or op.get("index")})
    except Exception:
        pass  # journaling must never break execution


def execute_ops(adapter, model_id: str, ops: List[Dict[str, Any]],
                txn_manager=None, atomic: bool = True,
                model_kind: str = "PDM") -> Dict[str, Any]:
    """Execute a flat op list against the adapter."""
    executed: List[str] = []
    results: List[Dict[str, Any]] = []
    txn = None
    try:
        if atomic and txn_manager is not None:
            txn = txn_manager.begin(model_id)
        for i, op in enumerate(ops):
            name = op.get("op")
            if name not in PATCH_OPS:
                raise InvalidParamsError(
                    f"Unknown operation '{name}' at position {i}. "
                    f"Valid operations: {', '.join(sorted(PATCH_OPS))}")
            old_props = _capture_old(adapter, model_id, op)
            res = _exec_one(adapter, model_id, op)
            executed.append(_describe(op, model_kind))
            results.append({"index": i, "op": name, "ok": True, "result": res})
            if txn is not None:
                _record(txn_manager, txn["txn_id"], model_id, op, res, old_props)
        if txn:
            txn_manager.commit(txn["txn_id"])
        return {"success": True, "executed": executed, "results": results,
                "summary": _summary(ops)}
    except Exception as exc:
        if txn:
            try:
                txn_manager.rollback(txn["txn_id"])
                return {"success": False, "error": {"code": "OPERATION_FAILED",
                                                    "message": f"{type(exc).__name__}: {exc}",
                                                    "details": {"executed_before_failure": executed}},
                        "rolled_back": True}
            except Exception as rb_exc:
                return {"success": False, "error": {"code": "TRANSACTION_ERROR",
                                                    "message": f"operation failed and rollback failed: {rb_exc}",
                                                    "details": {"original_error": str(exc),
                                                                "executed_before_failure": executed}},
                        "rolled_back": False}
        raise


def _exec_one(adapter, model_id: str, op: Dict[str, Any]) -> Any:
    name = op["op"]
    if name == "create_table":
        t = op["table"]
        return adapter.create_table(model_id, t["name"], t.get("code", ""),
                                    t.get("comment", ""))
    if name == "alter_table":
        return adapter.update_table(model_id, op["table"], op.get("updates") or {})
    if name == "rename_table":
        return adapter.update_table(model_id, op["table"],
                                    {"name": op.get("new_name"), "code": op.get("new_code")})
    if name == "drop_table":
        return adapter.delete_table(model_id, op["table"])
    if name == "add_column":
        return adapter.create_column(model_id, op["table"], op["column"])
    if name == "alter_column":
        return adapter.update_column(model_id, op["table"], op["column"], op.get("updates") or {})
    if name == "drop_column":
        return adapter.delete_column(model_id, op["table"], op["column"])
    if name == "create_primary_key":
        return adapter.create_primary_key(model_id, op["table"], op["columns"],
                                          name=op.get("name", ""), code=op.get("code", ""))
    if name == "drop_primary_key":
        return adapter.remove_primary_key(model_id, op["table"])
    if name == "create_reference":
        ref = op["reference"]
        return adapter.create_reference(
            model_id, ref["parent_table"], ref["child_table"],
            parent_columns=ref.get("parent_columns"), child_columns=ref.get("child_columns"),
            name=ref.get("name", ""), code=ref.get("code", ""),
            comment=ref.get("comment", ""), cardinality=ref.get("cardinality"),
            update_key=bool(ref.get("update_key", True)),
            parent_cardinality=ref.get("parent_cardinality"),
            dependent_role=ref.get("dependent_role"))
    if name == "alter_reference":
        return adapter.update_reference(model_id, op["reference"], op.get("updates") or {})
    if name == "drop_reference":
        return adapter.delete_reference(model_id, op["reference"])
    if name == "create_index":
        idx = op["index"]
        return adapter.create_index(model_id, op["table"], idx.get("columns") or [],
                                    name=idx.get("name", ""), code=idx.get("code", ""),
                                    unique=bool(idx.get("unique")), comment=idx.get("comment", ""))
    if name == "alter_index":
        return adapter.update_index(model_id, op["table"], op["index"], op.get("updates") or {})
    if name == "drop_index":
        return adapter.delete_index(model_id, op["table"], op["index"])
    if name == "create_domain":
        return adapter.create_domain(model_id, op["domain"])
    raise InvalidParamsError(f"Unknown operation '{name}'")


# ---------------------------------------------------------------------------
# Public entry points (called by MCP tools)
# ---------------------------------------------------------------------------

def create_database_schema(adapter, model_id: str, spec: Dict[str, Any],
                           dry_run: bool = False, atomic: bool = True,
                           txn_manager=None) -> Dict[str, Any]:
    # the plan must follow the target model's kind: a CDM/LDM builds
    # entities/attributes/identifiers and has no indexes at all
    info = adapter.get_model_info(model_id)  # validates the model exists
    kind = info.get("kind") or "PDM"
    ops = build_schema_ops(spec, model_kind=kind)
    if dry_run:
        return {"success": True, "dry_run": True,
                "plan": [_describe(o, kind) for o in ops],
                "operations": ops, "summary": _summary(ops)}
    result = execute_ops(adapter, model_id, ops, txn_manager=txn_manager,
                         atomic=atomic, model_kind=kind)
    result["dry_run"] = False
    return result


def apply_schema_patch(adapter, model_id: str, operations: List[Dict[str, Any]],
                       dry_run: bool = False, atomic: bool = True,
                       txn_manager=None) -> Dict[str, Any]:
    if not isinstance(operations, list) or not operations:
        raise InvalidParamsError("operations must be a non-empty JSON array")
    kind = _model_kind(adapter, model_id)
    if dry_run:
        plan = []
        for op in operations:
            if op.get("op") not in PATCH_OPS:
                raise InvalidParamsError(
                    f"Unknown operation '{op.get('op')}'. "
                    f"Valid operations: {', '.join(sorted(PATCH_OPS))}")
            plan.append(_describe(op, kind))
        return {"success": True, "dry_run": True, "plan": plan, "operations": operations,
                "summary": _summary(operations)}
    result = execute_ops(adapter, model_id, operations, txn_manager=txn_manager,
                         atomic=atomic, model_kind=kind)
    result["dry_run"] = False
    return result


def design_from_spec(adapter, spec: Dict[str, Any], model_id: Optional[str] = None,
                     dry_run: bool = False, atomic: bool = True, txn_manager=None) -> Dict[str, Any]:
    model = spec.get("model") or {}
    kind = (model.get("kind") or "PDM").upper()
    if kind not in ("PDM", "CDM", "LDM"):
        raise InvalidParamsError("spec.model.kind must be one of PDM, CDM, LDM")

    created_model = None
    if model_id is None:
        if dry_run:
            ops = build_schema_ops(spec, model_kind=kind)
            return {"success": True, "dry_run": True,
                    "would_create_model": {"kind": kind, "name": model.get("name"),
                                           "code": model.get("code"), "dbms": model.get("dbms")},
                    "plan": [_describe(o, kind) for o in ops], "operations": ops,
                    "summary": _summary(ops)}
        created_model = adapter.create_model(
            kind, name=model.get("name") or "New Model",
            code=model.get("code") or "", dbms=model.get("dbms"))
        model_id = created_model["model_id"]
    else:
        info = adapter.get_model_info(model_id)
        kind = info["kind"]

    ops = build_schema_ops(spec, model_kind=kind)
    if dry_run:
        return {"success": True, "dry_run": True, "model_id": model_id,
                "plan": [_describe(o, kind) for o in ops], "operations": ops,
                "summary": _summary(ops)}
    result = execute_ops(adapter, model_id, ops, txn_manager=txn_manager,
                         atomic=atomic, model_kind=kind)
    result["dry_run"] = False
    result["model_id"] = model_id
    if created_model:
        result["created_model"] = created_model
    return result
