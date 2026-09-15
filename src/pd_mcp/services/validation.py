"""Model validation.

Two layers:

1. ``validate_model`` - built-in structural checks (PK presence, duplicate
   codes, FK column type mismatch, redundant indexes, naming conventions).
2. ``check_database_design`` - a configurable rule engine.  A rule is
   ``{id, description, type, params?, severity?}``; machine-checkable rule
   types are listed in ``RULE_TYPES``.  Rules of unknown type are reported as
   *skipped* instead of failing silently, because designing rules is the AI's
   job - this server only executes them faithfully.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..errors import InvalidParamsError
from ..utils import is_snake_case

RULE_TYPES = {
    "TABLE_HAS_PK": "every table must have a primary key",
    "TABLE_CODE_PATTERN": "table code must match regex params.pattern",
    "TABLE_NAME_STYLE": "table name style: params.style = snake_case|upper_snake_case",
    "COLUMN_HAS_COMMENT": "every column must have a comment",
    "COLUMN_CODE_PATTERN": "column code must match regex params.pattern",
    "COLUMN_MANDATORY": "columns listed in params.columns must exist in every table and be mandatory",
    "REQUIRED_COLUMNS": "every table must contain the column codes listed in params.columns",
    "PK_NAME_PATTERN": "primary key name must match regex params.pattern",
    "FK_NAME_PATTERN": "reference name must match regex params.pattern",
    "INDEX_NAME_PATTERN": "index name must match regex params.pattern",
    "DATA_TYPE_ALLOWED": "column data types must be in params.allowed (list)",
    "NO_DUPLICATE_INDEX": "no two indexes on the same table may cover the same column set",
    "NO_EMPTY_COMMENT_TABLE": "tables must have a comment",
    "REGEX": "generic regex rule: params.target in (table_code, table_name, column_code, "
             "index_code, reference_code); params.pattern regex",
}


def _obj_code(c: Any) -> str:
    """Index/key column entries may be dicts (from adapters) or plain codes."""
    if isinstance(c, dict):
        return c.get("code") or c.get("expression") or c.get("name") or ""
    return str(c)


def _violation(rule_id: str, severity: str, obj: str, message: str) -> Dict[str, Any]:
    return {"rule_id": rule_id, "severity": severity, "object": obj, "message": message}


# ---------------------------------------------------------------------------
# Built-in validation
# ---------------------------------------------------------------------------

def validate_model(adapter, model_id: str) -> Dict[str, Any]:
    info = adapter.get_model_info(model_id)
    tables = adapter.list_tables(model_id)
    refs = adapter.list_references(model_id)
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def err(code: str, obj: str, msg: str):
        errors.append({"code": code, "object": obj, "message": msg})

    def warn(code: str, obj: str, msg: str):
        warnings.append({"code": code, "object": obj, "message": msg})

    tables_by_code: Dict[str, Dict[str, Any]] = {}
    seen_codes: Dict[str, str] = {}
    full_tables = {}
    for t in tables:
        full = adapter.get_table(model_id, t["ref"])
        full_tables[full["ref"]] = full
        tcode = full["code"]
        if not full["name"]:
            err("TABLE_NO_NAME", tcode, "Table has no Name")
        if not tcode:
            err("TABLE_NO_CODE", full["name"] or t["ref"], "Table has no Code")
        elif tcode in seen_codes:
            err("TABLE_DUPLICATE_CODE", tcode,
                f"Duplicate table code '{tcode}' (also used by {seen_codes[tcode]})")
        else:
            seen_codes[tcode] = full["name"]
        tables_by_code[tcode] = full
        if not full["columns"]:
            warn("TABLE_NO_COLUMNS", tcode, "Table has no columns")
        pk = full.get("primary_key")
        if not pk or not pk.get("columns"):
            err("TABLE_NO_PK", tcode, "Table has no primary key")
        for c in full["columns"]:
            col_id = f"{tcode}.{c['code']}"
            if not c["code"]:
                err("COLUMN_NO_CODE", col_id, "Column has no Code")
            if not c["data_type"]:
                err("COLUMN_NO_DATA_TYPE", col_id, "Column has no data type")
            if not c["comment"]:
                warn("COLUMN_NO_COMMENT", col_id, "Column has no comment")
        col_codes = [c["code"] for c in full["columns"]]
        dupes = {c for c in col_codes if col_codes.count(c) > 1}
        if dupes:
            err("COLUMN_DUPLICATE_CODE", tcode, f"Duplicate column codes: {sorted(dupes)}")
        # duplicate indexes with identical column sets
        idx_sets: Dict[tuple, str] = {}
        for idx in full["indexes"]:
            key = tuple(_obj_code(c) for c in idx["columns"])
            if key in idx_sets:
                warn("INDEX_DUPLICATE", f"{tcode}.{idx['code']}",
                     f"Index covers the same columns as '{idx_sets[key]}'")
            else:
                idx_sets[key] = idx["code"]
        pk_set = {(c["code"] if isinstance(c, dict) else c)
                  for c in pk["columns"]} if pk else set()
        for idx in full["indexes"]:
            if not idx.get("unique") and pk_set and \
                    {(_obj_code(c)) for c in idx["columns"]} == pk_set:
                warn("INDEX_REDUNDANT_WITH_PK", f"{tcode}.{idx['code']}",
                     "Non-unique index duplicates the primary key columns")

    for r in refs:
        parent = tables_by_code.get(r.get("parent_table"))
        child = tables_by_code.get(r.get("child_table"))
        rid = r.get("code") or r.get("ref")
        if not parent or not child:
            err("REF_DANGLING", rid, "Reference points to a missing table")
            continue
        if r.get("joins"):
            pairs = [(j.get("parent_column"), j.get("child_column"))
                     for j in r["joins"]]
        else:
            pairs = list(zip(r.get("parent_columns") or [],
                             r.get("child_columns") or []))
        if not pairs:
            warn("REF_NO_JOINS", rid, "Reference has no column joins")
            continue
        pcols = {c["code"]: c for c in parent["columns"]}
        ccols = {c["code"]: c for c in child["columns"]}
        for pc, cc in pairs:
            p, c = pcols.get(pc), ccols.get(cc)
            if p is None or c is None:
                err("REF_BROKEN_JOIN", rid, "Join references a missing column")
                continue
            if (p["data_type"] or "").lower() != (c["data_type"] or "").lower():
                err("REF_TYPE_MISMATCH", rid,
                    f"FK type mismatch: {child['code']}.{c['code']} "
                    f"({c['data_type']}) vs {parent['code']}.{p['code']} ({p['data_type']})")

    return {
        "model_id": model_id,
        "model_name": info.get("name"),
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {"errors": len(errors), "warnings": len(warnings),
                    "tables": len(tables), "references": len(refs)},
    }


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

def _match_pattern(rule: dict, value: str, obj: str, violations: list) -> None:
    pattern = (rule.get("params") or {}).get("pattern")
    if not pattern:
        return
    try:
        if not re.match(pattern, value or ""):
            violations.append(_violation(rule["id"], rule.get("severity", "warning"),
                                         obj, f"'{value}' does not match pattern '{pattern}'"))
    except re.error as exc:
        raise InvalidParamsError(f"Rule {rule['id']}: invalid regex pattern: {exc}")


def check_database_design(adapter, model_id: str, rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(rules, list) or not rules:
        raise InvalidParamsError("rules must be a non-empty JSON array")
    tables = adapter.list_tables(model_id)
    refs = adapter.list_references(model_id)
    violations: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    checked: set = set()

    full_tables = {t["ref"]: adapter.get_table(model_id, t["ref"]) for t in tables}

    for rule in rules:
        rid = rule.get("id") or f"RULE_{len(rules)}"
        rtype = (rule.get("type") or "").upper()
        params = rule.get("params") or {}
        severity = rule.get("severity", "warning")

        def add(obj, msg, sev=severity):
            violations.append(_violation(rid, sev, obj, msg))

        try:
            if rtype == "TABLE_HAS_PK":
                checked.add(rtype)
                for t in full_tables.values():
                    pk = t.get("primary_key")
                    if not pk or not pk.get("columns"):
                        add(t["code"], "table has no primary key", "error")
            elif rtype == "TABLE_CODE_PATTERN":
                checked.add(rtype)
                for t in full_tables.values():
                    _match_pattern(rule, t["code"], t["code"], violations)
            elif rtype == "TABLE_NAME_STYLE":
                checked.add(rtype)
                style = params.get("style", "snake_case")
                for t in full_tables.values():
                    if style == "snake_case" and not is_snake_case(t["code"]):
                        add(t["code"], f"table code '{t['code']}' is not snake_case")
            elif rtype == "COLUMN_HAS_COMMENT":
                checked.add(rtype)
                for t in full_tables.values():
                    for c in t["columns"]:
                        if not c["comment"]:
                            add(f"{t['code']}.{c['code']}", "column has no comment")
            elif rtype == "COLUMN_CODE_PATTERN":
                checked.add(rtype)
                for t in full_tables.values():
                    for c in t["columns"]:
                        _match_pattern(rule, c["code"], f"{t['code']}.{c['code']}", violations)
            elif rtype in ("COLUMN_MANDATORY", "REQUIRED_COLUMNS"):
                checked.add(rtype)
                cols = params.get("columns") or []
                for t in full_tables.values():
                    codes = {c["code"] for c in t["columns"]}
                    for want in cols:
                        if want not in codes:
                            add(t["code"], f"missing required column '{want}'", "error")
                        elif rtype == "COLUMN_MANDATORY":
                            col = next(c for c in t["columns"] if c["code"] == want)
                            if not col["mandatory"]:
                                add(f"{t['code']}.{want}", "required column is nullable")
            elif rtype == "PK_NAME_PATTERN":
                checked.add(rtype)
                for t in full_tables.values():
                    pk = t.get("primary_key")
                    if pk:
                        _match_pattern(rule, pk.get("name") or pk.get("code") or "",
                                       t["code"], violations)
            elif rtype == "FK_NAME_PATTERN":
                checked.add(rtype)
                for r in refs:
                    _match_pattern(rule, r.get("name") or "", r.get("code") or "", violations)
            elif rtype == "INDEX_NAME_PATTERN":
                checked.add(rtype)
                for t in full_tables.values():
                    for idx in t["indexes"]:
                        _match_pattern(rule, idx.get("name") or idx.get("code") or "",
                                       f"{t['code']}.{idx.get('code')}", violations)
            elif rtype == "DATA_TYPE_ALLOWED":
                checked.add(rtype)
                allowed = {str(a).upper() for a in (params.get("allowed") or [])}
                for t in full_tables.values():
                    for c in t["columns"]:
                        base = (c["data_type"] or "").split("(")[0].strip().upper()
                        if base and base not in allowed:
                            add(f"{t['code']}.{c['code']}",
                                f"data type '{c['data_type']}' not allowed", "error")
            elif rtype == "NO_DUPLICATE_INDEX":
                checked.add(rtype)
                for t in full_tables.values():
                    seen: Dict[tuple, str] = {}
                    for idx in t["indexes"]:
                        key = tuple(_obj_code(c) for c in idx["columns"])
                        if key in seen:
                            add(f"{t['code']}.{idx.get('code')}",
                                f"duplicate index of '{seen[key]}'")
                        else:
                            seen[key] = idx.get("code")
            elif rtype == "NO_EMPTY_COMMENT_TABLE":
                checked.add(rtype)
                for t in full_tables.values():
                    if not t.get("comment"):
                        add(t["code"], "table has no comment")
            elif rtype == "REGEX":
                checked.add(rtype)
                target = params.get("target", "table_code")
                if target == "table_code":
                    for t in full_tables.values():
                        _match_pattern(rule, t["code"], t["code"], violations)
                elif target == "table_name":
                    for t in full_tables.values():
                        _match_pattern(rule, t["name"], t["code"], violations)
                elif target == "column_code":
                    for t in full_tables.values():
                        for c in t["columns"]:
                            _match_pattern(rule, c["code"], f"{t['code']}.{c['code']}", violations)
                elif target == "index_code":
                    for t in full_tables.values():
                        for idx in t["indexes"]:
                            _match_pattern(rule, idx.get("code") or "", t["code"], violations)
                elif target == "reference_code":
                    for r in refs:
                        _match_pattern(rule, r.get("code") or "", r.get("code") or "", violations)
                else:
                    skipped.append({"rule_id": rid, "reason": f"unknown REGEX target '{target}'"})
            else:
                skipped.append({
                    "rule_id": rid,
                    "reason": f"rule type '{rtype}' is not machine-checkable; "
                              f"supported types: {', '.join(sorted(RULE_TYPES))}",
                })
        except InvalidParamsError:
            raise
        except Exception as exc:  # a broken rule must not kill the batch
            skipped.append({"rule_id": rid, "reason": f"evaluation error: {exc}"})

    errors = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] != "error"]
    return {
        "model_id": model_id,
        "passed": not violations,
        "errors": errors,
        "warnings": warnings,
        "violations": violations,
        "rules_checked": sorted(checked),
        "skipped": skipped,
        "summary": {"rules": len(rules), "violations": len(violations),
                    "errors": len(errors), "warnings": len(warnings)},
    }
