"""Model conversion used by the mock adapter (CDM -> LDM -> PDM).

The real COM adapter uses PowerDesigner's native GeneratePhysicalDataModel /
GenerateLogicalDataModel methods instead.  This module keeps a functional
mapping so the full course-design pipeline is testable without PowerDesigner.
"""

from __future__ import annotations

from ..errors import InvalidParamsError, NotSupportedError


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
            codes = []
            for c in full.get("columns", []):
                if c["ref"] in pk["columns"]:
                    codes.append(c["code"])
            if codes:
                adapter.create_primary_key(tid, created["ref"], codes,
                                           name=pk.get("name", ""), code=pk.get("code", ""))
        for idx in full.get("indexes", []):
            codes = []
            for c in full.get("columns", []):
                if c["ref"] in idx.get("columns", []):
                    codes.append(c["code"])
            if codes:
                adapter.create_index(tid, created["ref"], codes,
                                     name=idx.get("name", ""), code=idx.get("code", ""),
                                     unique=bool(idx.get("unique")), comment=idx.get("comment", ""))

    for r in refs:
        parent_ref = ref_map.get(r["parent_table"])
        child_ref = ref_map.get(r["child_table"])
        if not parent_ref or not child_ref:
            continue
        try:
            adapter.create_reference(
                tid, parent_ref, child_ref,
                parent_columns=r.get("parent_columns"),
                child_columns=r.get("child_columns"),
                name=r.get("name", ""), code=r.get("code", ""),
                comment=r.get("comment", ""), cardinality=r.get("cardinality"),
                update_key=False)
        except Exception:
            continue

    return {"source_model_id": model_id, "target_kind": target_kind,
            "target_model": adapter.get_model_info(tid)}
