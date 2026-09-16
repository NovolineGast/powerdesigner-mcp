"""In-memory implementation of :class:`PowerDesignerAdapter`.

Mirrors the observable behaviour of the real COM adapter (same dict shapes,
same PK/reference/index semantics) without requiring PowerDesigner.  Used by
the automated test-suite and for development on machines without PD.

Semantics mirrored from PowerDesigner:
* A column with ``primary=True`` belongs to the table's primary key; the PK
  is realised as a Key object with ``primary=True`` (PD creates it on demand).
* Creating a reference with ``update_key=True`` migrates the parent's PK
  columns into the child table as FK columns when they are missing.
* References hold joins (parent column <-> child column pairs).
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from ..errors import (
    ErrorCode,
    InvalidParamsError,
    ModelNotFoundError,
    ObjectNotFoundError,
    OperationFailedError,
    PdMcpError,
)
from .adapter import PowerDesignerAdapter
from .constants import MODEL_KINDS, kind_meta, supports_indexes


class _Store:
    def __init__(self, model_id: str, kind: str, name: str, code: str):
        self.id = model_id
        self.kind = kind
        self.name = name
        self.code = code
        self.comment = ""
        self.file_name: Optional[str] = None
        self.read_only = False
        self.dbms = ""
        self.tables: Dict[str, Dict[str, Any]] = {}
        self.references: Dict[str, Dict[str, Any]] = {}
        self.domains: Dict[str, Dict[str, Any]] = {}
        self.packages: Dict[str, Dict[str, Any]] = {}
        self.diagrams: List[str] = ["MainDiagram"]


class MockAdapter(PowerDesignerAdapter):
    def __init__(self, call_timeout_s: float = 300.0, **_ignored):
        self._lock = threading.RLock()
        self._models: Dict[str, _Store] = {}
        self._counter = 0
        self._connected = False
        self._info = {"product": "PowerDesigner (mock)", "version": "16.5-mock"}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _bump_counter(self, *refs: str) -> None:
        import re as _re
        for ref in refs:
            digits = _re.sub(r"\D", "", str(ref))
            if digits:
                self._counter = max(self._counter, int(digits))

    def _model(self, model_id: str) -> _Store:
        m = self._models.get(model_id)
        if m is None:
            raise ModelNotFoundError(model_id)
        return m

    @staticmethod
    def _find(by: Dict[str, Dict[str, Any]], ref: str, kind: str) -> Dict[str, Any]:
        if ref in by:
            return by[ref]
        for obj in by.values():
            if obj.get("code") == ref or obj.get("name") == ref:
                return obj
        raise ObjectNotFoundError(kind, ref)

    @staticmethod
    def _table(m: _Store, table_ref: str) -> Dict[str, Any]:
        return MockAdapter._find(m.tables, table_ref, "Table")

    @staticmethod
    def _column(t: Dict[str, Any], column_ref: str) -> Dict[str, Any]:
        return MockAdapter._find(t["columns"], column_ref, "Column")

    @staticmethod
    def _pk_key(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for key in t["keys"].values():
            if key["primary"]:
                return key
        return None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def connect(self) -> Dict[str, Any]:
        self._connected = True
        return {"attached": True, **self._info, "attach_mode": "mock"}

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def server_info(self) -> Dict[str, Any]:
        return {**self._info, "connected": self._connected, "attach_mode": "mock",
                "open_models": len(self._models)}

    # ------------------------------------------------------------------
    # model management
    # ------------------------------------------------------------------
    def list_models(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._model_info(m) for m in self._models.values()]

    def open_model(self, path: str, read_only: bool = False) -> Dict[str, Any]:
        from pathlib import Path
        p = Path(path)
        if not p.is_file():
            raise ObjectNotFoundError("File", path)
        with self._lock:
            raw = p.read_text(encoding="utf-8")
            if raw.lstrip().startswith('{"_mock_model"'):
                m = self._load_store(raw)
                m.read_only = read_only
                self._models[m.id] = m
                return self._model_info(m)
            suffix = p.suffix.lower()
            kind = {"": None, ".pdm": "PDM", ".cdm": "CDM", ".ldm": "LDM"}.get(suffix)
            if kind is None:
                raise InvalidParamsError(f"Unsupported model file type: {suffix}")
            mid = self._next_id("m")
            m = _Store(mid, kind, p.stem, p.stem)
            m.file_name = str(p.resolve())
            m.read_only = read_only
            m.dbms = "MySQL 5.0" if kind == "PDM" else ""
            self._models[mid] = m
            return self._model_info(m)

    def _load_store(self, raw: str) -> _Store:
        import json as _json
        data = _json.loads(raw)["_mock_model"]
        m = _Store(data["id"], data["kind"], data["name"], data["code"])
        m.comment = data.get("comment", "")
        m.file_name = data.get("file_name")
        m.dbms = data.get("dbms", "")
        m.tables = data.get("tables", {})
        m.references = data.get("references", {})
        m.domains = data.get("domains", {})
        m.packages = data.get("packages", {})
        m.diagrams = data.get("diagrams", ["MainDiagram"])
        # keep generated ids above restored ones
        self._bump_counter(m.id, *m.tables.keys(), *m.references.keys(),
                           *m.domains.keys(),
                           *[k for t in m.tables.values() for k in list(t["columns"]) + list(t["keys"]) + list(t["indexes"])])
        return m

    def _dump_store(self, m: _Store) -> str:
        import json as _json
        return _json.dumps({"_mock_model": {
            "id": m.id, "kind": m.kind, "name": m.name, "code": m.code,
            "comment": m.comment, "file_name": m.file_name, "dbms": m.dbms,
            "tables": m.tables, "references": m.references,
            "domains": m.domains, "packages": m.packages, "diagrams": m.diagrams,
        }}, ensure_ascii=False)

    def create_model(self, kind: str, name: str, code: str = "",
                     dbms: Optional[str] = None) -> Dict[str, Any]:
        kind = (kind or "").upper()
        if kind not in MODEL_KINDS:
            raise InvalidParamsError(f"Unknown model kind '{kind}'. Use PDM, CDM or LDM.")
        with self._lock:
            mid = self._next_id("m")
            m = _Store(mid, kind, name, code or name)
            if kind == "PDM":
                m.dbms = dbms or "MySQL 5.0"
            self._models[mid] = m
            return self._model_info(m)

    def save_model(self, model_id: str) -> Dict[str, Any]:
        m = self._model(model_id)
        if not m.file_name:
            raise OperationFailedError(
                "Model has never been saved; use save_model_as to choose a file first")
        from pathlib import Path
        Path(m.file_name).write_text(self._dump_store(m), encoding="utf-8")
        return {"model_id": model_id, "file": m.file_name, "saved": True}

    def save_model_as(self, model_id: str, path: str) -> Dict[str, Any]:
        m = self._model(model_id)
        ext = {"PDM": ".pdm", "CDM": ".cdm", "LDM": ".ldm"}[m.kind]
        if not path.lower().endswith((".pdm", ".cdm", ".ldm")):
            path = path + ext
        m.file_name = str(path)
        from pathlib import Path
        Path(m.file_name).write_text(self._dump_store(m), encoding="utf-8")
        return {"model_id": model_id, "file": m.file_name, "saved": True}

    def close_model(self, model_id: str, save: bool = False) -> Dict[str, Any]:
        m = self._model(model_id)
        if save and m.file_name:
            pass  # mock: nothing to write
        del self._models[model_id]
        return {"model_id": model_id, "closed": True, "saved": save}

    def get_model_info(self, model_id: str) -> Dict[str, Any]:
        return self._model_info(self._model(model_id))

    def _model_info(self, m: _Store) -> Dict[str, Any]:
        n_cols = sum(len(t["columns"]) for t in m.tables.values())
        n_keys = sum(len(t["keys"]) for t in m.tables.values())
        n_idx = sum(len(t["indexes"]) for t in m.tables.values())
        return {
            "model_id": m.id, "kind": m.kind, "name": m.name, "code": m.code,
            "comment": m.comment, "file": m.file_name, "read_only": m.read_only,
            "dbms": m.dbms,
            "object_counts": {
                "tables": len(m.tables), "columns": n_cols, "keys": n_keys,
                "references": len(m.references), "indexes": n_idx,
                "domains": len(m.domains), "packages": len(m.packages),
            },
        }

    # ------------------------------------------------------------------
    # packages
    # ------------------------------------------------------------------
    def list_packages(self, model_id: str) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        return [dict(p) for p in m.packages.values()]

    def get_package(self, model_id: str, package_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        return dict(self._find(m.packages, package_ref, "Package"))

    # ------------------------------------------------------------------
    # tables
    # ------------------------------------------------------------------
    def list_tables(self, model_id: str, package_ref: Optional[str] = None,
                    query: Optional[str] = None) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        out = []
        for t in m.tables.values():
            if query and query.lower() not in (t["code"] + t["name"]).lower():
                continue
            out.append(self._table_summary(t))
        return out

    def get_table(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        d = self._table_summary(t)
        d["columns"] = [self._column_dict(c) for c in t["columns"].values()]
        d["primary_key"] = self._key_dict(self._pk_key(t), t) if self._pk_key(t) else None
        d["keys"] = [self._key_dict(k, t) for k in t["keys"].values()]
        d["indexes"] = [self._index_dict(i, t) for i in t["indexes"].values()]
        d["references_out"] = [self._reference_dict(r, m) for r in m.references.values()
                               if r["parent_table"] == t["ref"]]
        d["references_in"] = [self._reference_dict(r, m) for r in m.references.values()
                              if r["child_table"] == t["ref"]]
        return d

    def create_table(self, model_id: str, name: str, code: str = "",
                     comment: str = "", columns: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        m = self._model(model_id)
        if m.read_only:
            raise PdMcpError(ErrorCode.READ_ONLY, "Model is read-only")
        if not name:
            raise InvalidParamsError("Table name is required")
        with self._lock:
            for t in m.tables.values():
                if t["code"] == (code or name):
                    raise PdMcpError(ErrorCode.CONFLICT,
                                     f"Table code '{code or name}' already exists")
            ref = self._next_id("t")
            t = {"ref": ref, "name": name, "code": code or name, "comment": comment,
                 "description": "", "columns": {}, "keys": {}, "indexes": {},
                 "package": None}
            m.tables[ref] = t
            for spec in (columns or []):
                self.create_column(model_id, ref, spec)
            return self.get_table(model_id, ref)

    def update_table(self, model_id: str, table_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        for field in ("name", "code", "comment", "description"):
            if updates.get(field) is not None:
                t[field] = updates[field]
        return self.get_table(model_id, t["ref"])

    def delete_table(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        # delete attached references and indexes first (like PD cascade)
        for r in [r for r in m.references.values()
                  if r["parent_table"] == t["ref"] or r["child_table"] == t["ref"]]:
            del m.references[r["ref"]]
        del m.tables[t["ref"]]
        return {"deleted": "table", "ref": t["ref"], "code": t["code"]}

    # ------------------------------------------------------------------
    # columns
    # ------------------------------------------------------------------
    def list_columns(self, model_id: str, table_ref: str,
                     query: Optional[str] = None) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        cols = [self._column_dict(c) for c in t["columns"].values()]
        if query:
            cols = [c for c in cols if query.lower() in (c["code"] + c["name"]).lower()]
        return cols

    def get_column(self, model_id: str, table_ref: str, column_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        return self._column_dict(self._column(t, column_ref))

    def create_column(self, model_id: str, table_ref: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        if m.read_only:
            raise PdMcpError(ErrorCode.READ_ONLY, "Model is read-only")
        t = self._table(m, table_ref)
        name = spec.get("name") or spec.get("code")
        if not name:
            raise InvalidParamsError("Column name is required")
        code = spec.get("code") or name
        # CDM/LDM attributes carry no primary flag (live-verified on PD 16.5):
        # the entity points at an Identifier instead, so honouring the flag
        # here would let the mock drift away from the real backend
        conceptual = not supports_indexes(m.kind)
        with self._lock:
            for c in t["columns"].values():
                if c["code"] == code:
                    raise PdMcpError(ErrorCode.CONFLICT,
                                     f"Column code '{code}' already exists in table '{t['code']}'")
            ref = self._next_id("c")
            col = {
                "ref": ref, "name": name, "code": code,
                "data_type": spec.get("data_type") or "",
                "length": spec.get("length"),
                "precision": spec.get("precision"),
                "mandatory": bool(spec.get("mandatory")),
                "default_value": spec.get("default_value"),
                "comment": spec.get("comment") or "",
                "description": spec.get("description") or "",
                "domain": spec.get("domain"),
                "primary": bool(spec.get("primary")) and not conceptual,
            }
            t["columns"][ref] = col
            if col["primary"]:
                self._add_to_pk(t, ref)
            return self._column_dict(col)

    def update_column(self, model_id: str, table_ref: str, column_ref: str,
                      updates: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        c = self._column(t, column_ref)
        for field in ("name", "code", "data_type", "length", "precision",
                      "mandatory", "default_value", "comment", "description", "domain"):
            if field in updates and updates[field] is not None:
                c[field] = updates[field]
        # see create_column: the primary flag only exists in a PDM
        if updates.get("primary") is not None and supports_indexes(m.kind):
            want = bool(updates["primary"])
            if want and not c["primary"]:
                c["primary"] = True
                self._add_to_pk(t, c["ref"])
            elif not want and c["primary"]:
                c["primary"] = False
                pk = self._pk_key(t)
                if pk and c["ref"] in pk["columns"]:
                    pk["columns"].remove(c["ref"])
        return self._column_dict(c)

    def delete_column(self, model_id: str, table_ref: str, column_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        c = self._column(t, column_ref)
        pk = self._pk_key(t)
        if pk and c["ref"] in pk["columns"]:
            pk["columns"].remove(c["ref"])
        for idx in t["indexes"].values():
            if c["ref"] in idx["columns"]:
                idx["columns"].remove(c["ref"])
        del t["columns"][c["ref"]]
        return {"deleted": "column", "ref": c["ref"], "code": c["code"]}

    # ------------------------------------------------------------------
    # keys
    # ------------------------------------------------------------------
    def list_keys(self, model_id: str, table_ref: str) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        return [self._key_dict(k, t) for k in t["keys"].values()]

    def create_primary_key(self, model_id: str, table_ref: str, columns: List[str],
                           name: str = "", code: str = "") -> Dict[str, Any]:
        m = self._model(model_id)
        if m.read_only:
            raise PdMcpError(ErrorCode.READ_ONLY, "Model is read-only")
        t = self._table(m, table_ref)
        if not columns:
            raise InvalidParamsError("Primary key needs at least one column")
        col_refs = []
        for c in columns:
            col = self._find(t["columns"], c, "Column")
            col_refs.append(col["ref"])
        with self._lock:
            # the replaced key is dropped, not just demoted: the COM backend
            # deletes the previous PK object/identifier when a new one is set
            old = self._pk_key(t)
            if old:
                del t["keys"][old["ref"]]
            ref = self._next_id("k")
            default_name = f"ID_{t['code']}" if not supports_indexes(m.kind) else f"PK_{t['code']}"
            key = {"ref": ref, "name": name or default_name,
                   "code": code or name or default_name,
                   "primary": True, "columns": col_refs, "table": t["ref"]}
            t["keys"][ref] = key
            # attributes have no primary flag in a CDM/LDM - the entity points
            # at the identifier instead
            if supports_indexes(m.kind):
                for c in t["columns"].values():
                    c["primary"] = c["ref"] in col_refs
            return self._key_dict(key, t)

    def remove_primary_key(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        pk = self._pk_key(t)
        if not pk:
            raise ObjectNotFoundError("PrimaryKey", f"table {t['code']}")
        del t["keys"][pk["ref"]]
        for c in t["columns"].values():
            c["primary"] = False
        return {"removed": "primary_key", "table": t["code"]}

    # ------------------------------------------------------------------
    # references
    # ------------------------------------------------------------------
    def list_references(self, model_id: str) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        return [self._reference_dict(r, m) for r in m.references.values()]

    def get_reference(self, model_id: str, reference_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        r = self._find(m.references, reference_ref, "Reference")
        return self._reference_dict(r, m)

    def create_reference(self, model_id: str, parent_table: str, child_table: str,
                         parent_columns: Optional[List[str]] = None,
                         child_columns: Optional[List[str]] = None,
                         name: str = "", code: str = "", comment: str = "",
                         cardinality: Optional[str] = None,
                         update_key: bool = True,
                         parent_cardinality: Optional[str] = None,
                         dependent_role: Optional[str] = None) -> Dict[str, Any]:
        m = self._model(model_id)
        if m.read_only:
            raise PdMcpError(ErrorCode.READ_ONLY, "Model is read-only")
        parent = self._table(m, parent_table)
        child = self._table(m, child_table)
        if parent["ref"] == child["ref"]:
            raise InvalidParamsError("Self-referencing tables are not supported by this tool")
        conceptual = not supports_indexes(m.kind)
        if conceptual:
            # CDM/LDM associations carry no column mapping or FK migration,
            # mirroring the COM backend where those arguments are ignored
            ref = self._next_id("r")
            r = {"ref": ref,
                 "name": name or code or f"rel_{parent['code']}_{child['code']}",
                 "code": code or name or f"rel_{parent['code']}_{child['code']}",
                 "comment": comment,
                 "parent_table": parent["ref"], "child_table": child["ref"],
                 "parent_columns": [], "child_columns": [],
                 "cardinality": cardinality or "0,n",
                 "parent_cardinality": parent_cardinality or "1,1",
                 "dependent_role": dependent_role or "",
                 "update_key": update_key}
            with self._lock:
                m.references[ref] = r
            return self._reference_dict(r, m)
        # resolve join columns
        if parent_columns:
            pcols = [self._find(parent["columns"], c, "Column") for c in parent_columns]
        else:
            pk = self._pk_key(parent)
            if not pk:
                raise OperationFailedError(
                    f"Parent table '{parent['code']}' has no primary key; "
                    "pass parent_columns/child_columns explicitly")
            pcols = [parent["columns"][c] for c in pk["columns"]]
        if child_columns:
            if len(child_columns) != len(pcols):
                raise InvalidParamsError("parent_columns and child_columns must have same length")
            ccols = [self._find(child["columns"], c, "Column") for c in child_columns]
        elif update_key:
            ccols = []
            for pc in pcols:
                existing = next((c for c in child["columns"].values() if c["code"] == pc["code"]), None)
                if existing is None:
                    spec = {"name": pc["name"], "code": pc["code"],
                            "data_type": pc["data_type"], "length": pc["length"],
                            "precision": pc["precision"], "mandatory": False,
                            "comment": f"FK -> {parent['code']}.{pc['code']}"}
                    existing = self.create_column(model_id, child["ref"], spec)
                    existing = child["columns"][existing["ref"]]
                ccols.append(existing)
        else:
            raise InvalidParamsError("child_columns required when update_key is False")

        with self._lock:
            ref = self._next_id("r")
            r = {"ref": ref,
                 "name": name or f"FK_{child['code']}_{parent['code']}",
                 "code": code or name or f"FK_{child['code']}_{parent['code']}",
                 "comment": comment,
                 "parent_table": parent["ref"], "child_table": child["ref"],
                 "parent_columns": [c["ref"] for c in pcols],
                 "child_columns": [c["ref"] for c in ccols],
                 "cardinality": cardinality or "0,n",
                 "parent_cardinality": parent_cardinality or "1,1",
                 "dependent_role": dependent_role or "",
                 "update_key": update_key}
            m.references[ref] = r
            return self._reference_dict(r, m)

    def update_reference(self, model_id: str, reference_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        r = self._find(m.references, reference_ref, "Reference")
        for field in ("name", "code", "comment", "cardinality",
                      "parent_cardinality", "dependent_role"):
            if updates.get(field) is not None:
                r[field] = updates[field]
        return self._reference_dict(r, m)

    def delete_reference(self, model_id: str, reference_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        r = self._find(m.references, reference_ref, "Reference")
        del m.references[r["ref"]]
        return {"deleted": "reference", "ref": r["ref"], "code": r["code"]}

    # ------------------------------------------------------------------
    # indexes
    # ------------------------------------------------------------------
    def list_indexes(self, model_id: str, table_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        out = []
        for t in m.tables.values():
            if table_ref and t["ref"] != self._table(m, table_ref)["ref"]:
                continue
            for idx in t["indexes"].values():
                out.append(self._index_dict(idx, t))
        return out

    def get_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        t = self._table(m, table_ref)
        idx = self._find(t["indexes"], index_ref, "Index")
        return self._index_dict(idx, t)

    def _require_index_support(self, m) -> None:
        """Mirror the COM backend: indexes exist in a PDM only.

        Without this the mock happily accepts indexes on a CDM and the failure
        only shows up on a real PowerDesigner run.
        """
        if not supports_indexes(m.kind):
            raise InvalidParamsError(
                f"Indexes do not exist in a {m.kind} model - they are a physical "
                "(PDM) concept. Convert to a PDM first, or use keys/identifiers.")

    def create_index(self, model_id: str, table_ref: str, columns: List[str],
                     name: str = "", code: str = "", unique: bool = False,
                     comment: str = "") -> Dict[str, Any]:
        m = self._model(model_id)
        if m.read_only:
            raise PdMcpError(ErrorCode.READ_ONLY, "Model is read-only")
        self._require_index_support(m)
        t = self._table(m, table_ref)
        if not columns:
            raise InvalidParamsError("Index needs at least one column")
        col_refs = []
        for c in columns:
            col_refs.append(self._find(t["columns"], c, "Column")["ref"])
        with self._lock:
            ref = self._next_id("i")
            idx = {"ref": ref, "name": name or f"idx_{t['code']}_{'_'.join(columns)}",
                   "code": code or name or f"idx_{t['code']}_{'_'.join(columns)}",
                   "unique": unique, "comment": comment,
                   "columns": col_refs, "table": t["ref"]}
            t["indexes"][ref] = idx
            return self._index_dict(idx, t)

    def update_index(self, model_id: str, table_ref: str, index_ref: str,
                     updates: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        self._require_index_support(m)
        t = self._table(m, table_ref)
        idx = self._find(t["indexes"], index_ref, "Index")
        for field in ("name", "code", "comment", "unique"):
            if field in updates and updates[field] is not None:
                idx[field] = updates[field]
        if updates.get("columns"):
            idx["columns"] = [self._find(t["columns"], c, "Column")["ref"]
                              for c in updates["columns"]]
        return self._index_dict(idx, t)

    def delete_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        self._require_index_support(m)
        t = self._table(m, table_ref)
        idx = self._find(t["indexes"], index_ref, "Index")
        del t["indexes"][idx["ref"]]
        return {"deleted": "index", "ref": idx["ref"], "code": idx["code"]}

    # ------------------------------------------------------------------
    # domains
    # ------------------------------------------------------------------
    def list_domains(self, model_id: str) -> List[Dict[str, Any]]:
        m = self._model(model_id)
        return [dict(d) for d in m.domains.values()]

    def get_domain(self, model_id: str, domain_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        return dict(self._find(m.domains, domain_ref, "Domain"))

    def create_domain(self, model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        name = spec.get("name") or spec.get("code")
        if not name:
            raise InvalidParamsError("Domain name is required")
        ref = self._next_id("d")
        dom = {"ref": ref, "name": name, "code": spec.get("code") or name,
               "data_type": spec.get("data_type") or "", "length": spec.get("length"),
               "precision": spec.get("precision"), "comment": spec.get("comment") or "",
               "default_value": spec.get("default_value"), "mandatory": bool(spec.get("mandatory"))}
        m.domains[ref] = dom
        return dict(dom)

    # ------------------------------------------------------------------
    # native check / ddl / conversion
    # ------------------------------------------------------------------
    def native_check_model(self, model_id: str) -> Dict[str, Any]:
        m = self._model(model_id)
        return {"model_id": model_id, "engine": "powerdesigner-mock",
                "messages": [], "errors": 0, "warnings": 0}

    def generate_database(self, model_id: str, output_path: str,
                          options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from ..services.ddl_preview import render_ddl
        m = self._model(model_id)
        if m.kind != "PDM":
            raise NotSupportedError("generate_database requires a PDM model")
        sql = render_ddl(self, model_id)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(sql)
        return {"model_id": model_id, "file": str(output_path), "bytes": len(sql.encode()),
                "dbms": m.dbms, "engine": "powerdesigner-mock", "sql": sql}

    def convert_model(self, model_id: str, target_kind: str,
                      dbms: Optional[str] = None) -> Dict[str, Any]:
        from ..services.conversion import convert_model as svc_convert
        return svc_convert(self, model_id, target_kind, dbms)

    # ------------------------------------------------------------------
    # tracked object utilities
    # ------------------------------------------------------------------
    def delete_object_by_ref(self, model_id: str, kind: str, obj_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        if kind == "table":
            return self.delete_table(model_id, obj_ref)
        if kind == "column":
            t = next(t for t in m.tables.values() if obj_ref in t["columns"])
            return self.delete_column(model_id, t["ref"], obj_ref)
        if kind == "reference":
            return self.delete_reference(model_id, obj_ref)
        if kind == "domain":
            del m.domains[self._find(m.domains, obj_ref, "Domain")["ref"]]
            return {"deleted": kind, "ref": obj_ref}
        raise InvalidParamsError(f"Cannot delete object kind '{kind}'")

    def update_object_props(self, model_id: str, kind: str, obj_ref: str,
                            props: Dict[str, Any]) -> Dict[str, Any]:
        m = self._model(model_id)
        if kind == "table":
            return self.update_table(model_id, obj_ref, props)
        if kind == "reference":
            return self.update_reference(model_id, obj_ref, props)
        raise InvalidParamsError(f"Cannot update object kind '{kind}' via generic props")

    def get_object_props(self, model_id: str, kind: str, obj_ref: str) -> Dict[str, Any]:
        m = self._model(model_id)
        if kind == "table":
            t = self._table(m, obj_ref)
            return {k: t[k] for k in ("name", "code", "comment")}
        if kind == "reference":
            r = self._find(m.references, obj_ref, "Reference")
            return {k: r[k] for k in ("name", "code", "comment")}
        raise InvalidParamsError(f"Cannot read object kind '{kind}' via generic props")

    # ------------------------------------------------------------------
    # dict renderers
    # ------------------------------------------------------------------
    def _column_dict(self, c: Dict[str, Any]) -> Dict[str, Any]:
        return dict(c)

    def _key_dict(self, k: Dict[str, Any], table: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        d = dict(k)
        if table is not None:
            by_ref = table["columns"]
            d["columns"] = [self._column_dict(by_ref[c]) if c in by_ref else {"ref": c}
                            for c in k.get("columns", [])]
        return d

    def _index_dict(self, i: Dict[str, Any],
                    table: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        d = dict(i)
        if table is not None:
            by_ref = table["columns"]
            d["columns"] = [self._column_dict(by_ref[c]) if c in by_ref else {"ref": c}
                            for c in i.get("columns", [])]
        return d

    def _reference_dict(self, r: Dict[str, Any],
                        model: Optional[_Store] = None) -> Dict[str, Any]:
        d = dict(r)
        if model is not None:
            pt = model.tables.get(r["parent_table"])
            ct = model.tables.get(r["child_table"])
            d["parent_table"] = pt["code"] if pt else r["parent_table"]
            d["child_table"] = ct["code"] if ct else r["child_table"]
            d["parent_table_ref"] = r["parent_table"]
            d["child_table_ref"] = r["child_table"]
            pcols = pt["columns"] if pt else {}
            ccols = ct["columns"] if ct else {}
            pc = [pcols[x]["code"] if x in pcols else x for x in r["parent_columns"]]
            cc = [ccols[x]["code"] if x in ccols else x for x in r["child_columns"]]
            d["parent_columns"] = pc
            d["child_columns"] = cc
            d["joins"] = [{"parent_column": a, "child_column": b}
                          for a, b in zip(pc, cc)]
        return d

    def _table_summary(self, t: Dict[str, Any]) -> Dict[str, Any]:
        return {"ref": t["ref"], "name": t["name"], "code": t["code"],
                "comment": t["comment"], "column_count": len(t["columns"]),
                "index_count": len(t["indexes"]), "key_count": len(t["keys"])}

    def _add_to_pk(self, t: Dict[str, Any], col_ref: str) -> None:
        pk = self._pk_key(t)
        if pk is None:
            ref = self._next_id("k")
            pk = {"ref": ref, "name": f"PK_{t['code']}", "code": f"PK_{t['code']}",
                  "primary": True, "columns": [], "table": t["ref"]}
            t["keys"][ref] = pk
        if col_ref not in pk["columns"]:
            pk["columns"].append(col_ref)
