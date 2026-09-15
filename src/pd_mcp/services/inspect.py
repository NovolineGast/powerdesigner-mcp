"""Model inspection: snapshots, inspect_schema, compare_model.

All output is plain JSON designed to stay inside an LLM context window:
``summary`` mode returns one line per table, ``detail`` mode adds columns,
keys, indexes and references.  ``inspect_schema`` supports pagination and
per-table queries.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..utils import paginate


def model_snapshot(adapter, model_id: str, mode: str = "summary",
                   include_domains: bool = True) -> Dict[str, Any]:
    info = adapter.get_model_info(model_id)
    tables = adapter.list_tables(model_id)
    refs = adapter.list_references(model_id)
    snapshot: Dict[str, Any] = {"model": info, "mode": mode}

    if mode == "summary":
        table_codes = {t["ref"]: t["code"] for t in tables}
        snapshot["tables"] = [{"code": t["code"], "name": t["name"],
                               "columns": t.get("column_count")} for t in tables]
        snapshot["references"] = [
            {"code": r.get("code"),
             "parent": table_codes.get(r.get("parent_table"), r.get("parent_table")),
             "child": table_codes.get(r.get("child_table"), r.get("child_table"))}
            for r in refs
        ]
    else:
        detail = []
        for t in tables:
            full = adapter.get_table(model_id, t["ref"])
            detail.append({
                "code": full["code"], "name": full["name"], "comment": full.get("comment"),
                "columns": [{"code": c["code"], "name": c["name"],
                             "data_type": c["data_type"],
                             "length": c.get("length"), "precision": c.get("precision"),
                             "mandatory": c.get("mandatory"),
                             "default": c.get("default_value"),
                             "primary": c.get("primary"),
                             "comment": c.get("comment")} for c in full.get("columns", [])],
                "primary_key": full.get("primary_key"),
                "indexes": full.get("indexes", []),
                "references_out": full.get("references_out", []),
                "references_in": full.get("references_in", []),
            })
        snapshot["tables"] = detail
        snapshot["references"] = refs
    if include_domains:
        snapshot["domains"] = adapter.list_domains(model_id)
    return snapshot


def inspect_schema(adapter, model_id: str, mode: str = "summary",
                   table: Optional[str] = None, page: int = 1, page_size: int = 50,
                   max_page_size: int = 200) -> Dict[str, Any]:
    info = adapter.get_model_info(model_id)
    tables = adapter.list_tables(model_id)
    refs = adapter.list_references(model_id)
    table_by_ref = {t["ref"]: t["code"] for t in tables}

    if table:
        full = adapter.get_table(model_id, table)
        rows: List[Dict[str, Any]] = [full]
    else:
        rows = []
        for t in tables:
            if mode == "detail":
                rows.append(adapter.get_table(model_id, t["ref"]))
            else:
                rows.append(t)
    paged = paginate(rows, page=page, page_size=page_size, max_page_size=max_page_size)

    fk_view = [{"code": r.get("code"),
                "parent": table_by_ref.get(r.get("parent_table"), r.get("parent_table")),
                "child": table_by_ref.get(r.get("child_table"), r.get("child_table")),
                "cardinality": r.get("cardinality")} for r in refs]
    return {
        "model": {"model_id": info["model_id"], "name": info["name"],
                  "kind": info["kind"], "dbms": info.get("dbms")},
        "mode": mode,
        "tables": paged,
        "foreign_keys": fk_view,
    }


def compare_model(adapter, model_a: str, model_b: str) -> Dict[str, Any]:
    """Structural diff of two open models (by object code)."""
    a_info, b_info = adapter.get_model_info(model_a), adapter.get_model_info(model_b)

    def collect(model_id: str):
        tables = {}
        for t in adapter.list_tables(model_id):
            full = adapter.get_table(model_id, t["ref"])
            tables[full["code"]] = full
        refs = {r.get("code"): r for r in adapter.list_references(model_id)}
        domains = {d["code"]: d for d in adapter.list_domains(model_id)}
        return tables, refs, domains

    ta, ra, da = collect(model_a)
    tb, rb, db = collect(model_b)

    diff: Dict[str, Any] = {
        "model_a": {"model_id": model_a, "name": a_info["name"]},
        "model_b": {"model_id": model_b, "name": b_info["name"]},
        "tables_added": sorted(set(tb) - set(ta)),
        "tables_removed": sorted(set(ta) - set(tb)),
        "tables_modified": [],
        "columns_added": {}, "columns_removed": {}, "columns_modified": {},
        "references_added": sorted(set(rb) - set(ra)),
        "references_removed": sorted(set(ra) - set(rb)),
        "domains_added": sorted(set(db) - set(da)),
        "domains_removed": sorted(set(da) - set(db)),
    }

    for code in sorted(set(ta) & set(tb)):
        A, B = ta[code], tb[code]
        ca = {c["code"]: c for c in A["columns"]}
        cb = {c["code"]: c for c in B["columns"]}
        if set(ca) != set(cb) or _col_props(ca) != _col_props(cb) or \
                A.get("comment") != B.get("comment"):
            diff["tables_modified"].append(code)
        added = sorted(set(cb) - set(ca))
        removed = sorted(set(ca) - set(cb))
        modified = []
        for c in sorted(set(ca) & set(cb)):
            if _col_props(ca[c]) != _col_props(cb[c]):
                modified.append(c)
        if added:
            diff["columns_added"][code] = added
        if removed:
            diff["columns_removed"][code] = removed
        if modified:
            diff["columns_modified"][code] = modified

    changed = [k for k in diff if diff[k] and isinstance(diff[k], (list, dict))]
    diff["has_differences"] = bool(changed)
    return diff


def _col_props(c: Dict[str, Any]) -> tuple:
    return (c.get("data_type"), c.get("length"), c.get("precision"),
            bool(c.get("mandatory")), c.get("default_value"), c.get("comment"))
