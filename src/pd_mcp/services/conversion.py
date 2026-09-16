"""Model conversion used by the mock adapter (CDM -> LDM -> PDM).

The real COM adapter uses PowerDesigner's native GeneratePhysicalDataModel /
GenerateLogicalDataModel methods instead.  This module keeps a functional
mapping so the full course-design pipeline is testable without PowerDesigner.
"""

from __future__ import annotations

from ..errors import InvalidParamsError, NotSupportedError


def _member_refs(entry: Dict[str, Any]) -> set[str]:
    """Collect member refs from a key/index dict.

    ``get_table`` renders key and index members as full column dicts while the
    stored form holds refs; both shapes have to be accepted or the converted
    model silently loses its primary keys.
    """
    return {c["ref"] if isinstance(c, dict) else c for c in entry.get("columns", [])}


def convert_model(adapter, model_id: str, target_kind: str,
                  dbms: str | None = None) -> dict:
    source = adapter.get_model_info(model_id)
    src_kind = (source.get("kind") or "").upper()
    target_kind = (target_kind or "").upper()

    allowed = {("CDM", "LDM"), ("CDM", "PDM"), ("LDM", "PDM")}
    if (src_kind, target_kind) not in allowed:
        raise NotSupportedError(
            f"Conversion {src_kind} -> {target_kind} is not supported "
            "(supported: CDM->LDM, CDM->PDM, LDM->PDM)")

    # CDM/LDM entities are represented through the table/column store in the
    # mock adapter; the mapping below therefore works uniformly.
    tables = adapter.list_tables(model_id)
    refs = adapter.list_references(model_id) if src_kind != "PDM" else []

    target = adapter.create_model(
        target_kind,
        name=f"{source['name']} ({target_kind})",
        code=f"{source['code']}_{target_kind}",
        dbms=dbms if target_kind == "PDM" else None,
    )
    tid = target["model_id"]

    ref_map: dict[str, str] = {}
    for t in tables:
        full = adapter.get_table(model_id, t["ref"])
        created = adapter.create_table(tid, full["name"], full["code"], full.get("comment", ""))
        ref_map[full["code"]] = created["ref"]
        for c in full.get("columns", []):
            spec = {k: c.get(k) for k in ("name", "code", "data_type", "length",
                                          "precision", "mandatory", "default_value",
                                          "comment", "primary")}
            adapter.create_column(tid, created["ref"], spec)
        pk = full.get("primary_key")
        if pk and pk.get("columns"):
            pk_refs = _member_refs(pk)
            codes = [c["code"] for c in full.get("columns", []) if c["ref"] in pk_refs]
            if codes:
                adapter.create_primary_key(tid, created["ref"], codes,
                                           name=pk.get("name", ""), code=pk.get("code", ""))
        for idx in full.get("indexes", []):
            idx_refs = _member_refs(idx)
            codes = [c["code"] for c in full.get("columns", []) if c["ref"] in idx_refs]
            if codes:
                adapter.create_index(tid, created["ref"], codes,
                                     name=idx.get("name", ""), code=idx.get("code", ""),
                                     unique=bool(idx.get("unique")), comment=idx.get("comment", ""))

    for r in refs:
        parent_ref = ref_map.get(r["parent_table"])
        child_ref = ref_map.get(r["child_table"])
        if not parent_ref or not child_ref:
            continue
        # A conceptual relationship carries no column mapping: PowerDesigner
        # migrates the parent's identifier attributes into the child only while
        # generating the target model (live-verified - the .cdm holds each
        # identifier attribute exactly once).  No explicit columns therefore
        # means "derive them from the parent primary key", which is what
        # update_key=true does.
        explicit = bool(r.get("parent_columns")) and bool(r.get("child_columns"))
        try:
            adapter.create_reference(
                tid, parent_ref, child_ref,
                parent_columns=r.get("parent_columns") or None,
                child_columns=r.get("child_columns") or None,
                name=r.get("name", ""), code=r.get("code", ""),
                comment=r.get("comment", ""), cardinality=r.get("cardinality"),
                update_key=not explicit,
                parent_cardinality=r.get("parent_cardinality"),
                dependent_role=r.get("dependent_role"))
        except Exception:
            continue

    return {"source_model_id": model_id, "target_kind": target_kind,
            "target_model": adapter.get_model_info(tid)}
