"""Real PowerDesigner COM Automation adapter (PowerDesigner 16.5 verified).

Every COM access is funnelled through a single dedicated STA thread
(:class:`ComDispatcher`) because:

* PowerDesigner is an out-of-process LocalServer32 registered only in the
  32-bit registry view on this machine, so a 64-bit Python client cannot
  ``CoCreateInstance`` it directly - it attaches through the ROT
  (``GetActiveObject``) after PowerDesigner is running;
* COM single-threaded-apartment rules require all calls on one thread;
* PowerDesigner itself is not thread-safe.

Member names verified against ``Interop.PdCommon.dll`` / ``Interop.PdPDM.dll``
metadata (PD 16.5) and the official OLE automation samples, e.g.:
``IApplication.{CreateModel,OpenModel,Models,ActiveModel,Version,InteractiveMode,
BeginTransaction,EndTransaction,CancelTransaction,HomeDirectory}``,
``PdPDM.Model.{Tables,References,Domains,Packages,DBMS,ChangeDBMS,Save,Close,
CheckModel,GenerateDatabase}``, ``Table.{Columns,Keys,Indexes}``,
``Column.{DataType,Length,Precision,Mandatory,DefaultValue,Comment,Primary,Domain}``,
``Key.{Primary,Columns}``, ``Reference.{ParentTable,ChildTable,ParentKey,Joins,
UpdateReferenceJoins,Mandatory,ParentRole,ChildRole}``,
``ReferenceJoin.{ParentTableColumn,ChildTableColumn}``,
``index.{Unique,IndexColumns}``, ``IndexColumn.{Column}``,
``BaseObject.SetNameAndCode/FindChildByCode/ObjectID``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import ServerConfig, get_config
from ..errors import (
    ComUnavailableError,
    ErrorCode,
    InvalidParamsError,
    ModelNotFoundError,
    ObjectNotFoundError,
    OperationFailedError,
    PdMcpError,
)
from .adapter import PowerDesignerAdapter
from .constants import (
    CDM_CLASSES,
    LDM_CLASSES,
    MODEL_KINDS,
    PDM_CLASSES,
    PD_CDM_DOMAIN,
    PD_LDM_DOMAIN,
    PD_PDM_DOMAIN,
    conceptual_data_type,
    kind_meta,
    supports_indexes,
    uses_conceptual_relationships,
)

_PROGID_DEFAULT = "PowerDesigner.Application"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# COM dispatcher thread
# ---------------------------------------------------------------------------

class ComDispatcher:
    """Runs every PowerDesigner COM call on one dedicated STA thread."""

    def __init__(self, startup_timeout_s: float, call_timeout_s: float):
        self._queue: "queue.Queue[tuple]" = None  # created lazily (py3.13 queue)
        import queue as _queue
        self._queue = _queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._app = None
        self._we_started = False
        self._progid = _PROGID_DEFAULT
        self._startup_timeout_s = startup_timeout_s
        self._call_timeout_s = call_timeout_s

    # -- lifecycle ---------------------------------------------------------
    def start_and_attach(self, progid: str, attach_mode: str, pd_exe: Optional[str],
                         visible: bool) -> Dict[str, Any]:
        self._progid = progid
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pd-com-dispatcher")
        self._thread.start()
        if not self._ready.wait(timeout=self._startup_timeout_s + 10):
            raise ComUnavailableError("PowerDesigner COM thread did not start")
        if self._startup_error:
            raise ComUnavailableError(f"Cannot attach to PowerDesigner: {self._startup_error}")

        result = self.call(lambda: self._attach(progid, attach_mode, pd_exe, visible),
                           timeout=self._startup_timeout_s)
        return result

    def _run(self) -> None:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception as exc:  # pragma: no cover
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        import queue as _queue
        while True:
            fn, result_q = self._queue.get()
            if fn is None:
                return
            try:
                result_q.put((True, fn()))
            except BaseException as exc:  # noqa: BLE001 - forwarded to caller
                result_q.put((False, exc))

    # -- call --------------------------------------------------------------
    def call(self, fn, timeout: Optional[float] = None):
        import queue as _queue
        result_q: "queue.Queue[tuple]" = _queue.Queue()
        self._queue.put((fn, result_q))
        timeout = timeout or self._call_timeout_s
        try:
            ok, result = result_q.get(timeout=timeout)
        except _queue.Empty:
            raise PdMcpError(
                ErrorCode.COM_TIMEOUT,
                f"PowerDesigner COM call timed out after {timeout:.0f}s. "
                "A modal dialog may be open in PowerDesigner.")
        if ok:
            return result
        raise self._map_com_error(result)

    @staticmethod
    def _map_com_error(exc: BaseException) -> PdMcpError:
        import pywintypes  # noqa: F401  (import guarded by caller context)
        if isinstance(exc, Exception):
            text = str(exc)
            if "class not registered" in text.lower() or "invalid class string" in text.lower():
                return ComUnavailableError(
                    "PowerDesigner Application COM class is not available to this "
                    f"process ({text}). Start PowerDesigner or check registration.")
            return OperationFailedError(f"COM call failed: {type(exc).__name__}: {text}")
        return OperationFailedError(f"COM call failed: {exc}")

    # -- attach (runs on dispatcher thread) ---------------------------------
    def _attach(self, progid: str, attach_mode: str, pd_exe: Optional[str],
                visible: bool) -> Dict[str, Any]:
        import pythoncom
        import win32com.client

        details: Dict[str, Any] = {"progid": progid}

        def try_get_active():
            try:
                ptr = pythoncom.GetActiveObject(progid)
                self._app = win32com.client.Dispatch(ptr)
                details["attach"] = "running-instance"
                return True
            except Exception:
                return False

        def try_co_create():
            try:
                self._app = win32com.client.Dispatch(progid)
                details["attach"] = "cocreate"
                return True
            except Exception:
                return False

        def launch_and_attach():
            exe = pd_exe or self._find_pd_exe()
            if not exe or not Path(exe).is_file():
                raise ComUnavailableError(
                    "PowerDesigner executable not found. Set PDMCP_PD_EXE or "
                    "install PowerDesigner.")
            import subprocess
            subprocess.Popen([exe], cwd=str(Path(exe).parent))
            details["launched"] = exe
            deadline = time.time() + self._startup_timeout_s
            while time.time() < deadline:
                if try_get_active():
                    details["attach"] = "launched+rot"
                    return True
                time.sleep(1.0)
            return False

        order = {
            "auto": [try_get_active, try_co_create, launch_and_attach],
            "attach": [try_get_active],
            "launch": [try_get_active, launch_and_attach],
            "new": [try_co_create],
        }[attach_mode if attach_mode in ("auto", "attach", "launch", "new") else "auto"]

        for step in order:
            if step():
                self._we_started = "launched" in str(details.get("attach", ""))
                # suppress modal dialogs where supported
                try:
                    self._app.InteractiveMode = False
                except Exception:
                    pass
                try:
                    version = str(self._app.Version)
                except Exception:
                    version = "unknown"
                details["version"] = version
                return details
        raise ComUnavailableError(
            "Could not attach to PowerDesigner (tried: "
            + ", ".join(f.__name__ for f in order) + "). Is PowerDesigner installed?")

    @staticmethod
    def _find_pd_exe() -> Optional[str]:
        try:
            import winreg
            for base in (r"SOFTWARE\Classes\CLSID", r"SOFTWARE\Classes\WOW6432Node\CLSID"):
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    rf"{base}\{{B6988F4F-A312-45D8-9BFF-29ABF70844E1}}\LocalServer32") as k:
                    value = winreg.QueryValueEx(k, "")[0]
                    path = value.strip('"').split(" /")[0].strip()
                    if path.lower().endswith(".exe"):
                        return path
        except OSError:
            pass
        return None

    # -- shutdown -----------------------------------------------------------
    def shutdown(self, auto_quit: str) -> None:
        if self._app is None:
            return
        should_quit = auto_quit == "always" or \
            (auto_quit == "if-launched" and self._we_started)
        if should_quit:
            try:
                self.call(self._close_app, timeout=60)
            except Exception:
                pass
        # Release the proxy on this apartment.  Letting the main thread drop it
        # during interpreter teardown raises CO_E_NOTINITIALIZED (0x800401f0) -
        # reported by the host as a fatal exception.
        try:
            self.call(self._drop_app, timeout=10)
        except Exception:
            pass

    def _drop_app(self) -> None:
        self._app = None

    def _close_app(self) -> bool:
        """Application has no Quit method; close the main window instead."""
        import ctypes
        hwnd = None
        try:
            hwnd = int(self._app.MainWindowHandle)
        except Exception:
            pass
        if hwnd:
            WM_CLOSE = 0x0010
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return True
        return False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ComAdapter(PowerDesignerAdapter):
    def __init__(self, config: Optional[ServerConfig] = None, **_ignored):
        self._config = config or get_config()
        self._disp: Optional[ComDispatcher] = None
        self._app = None
        self._models: Dict[str, Dict[str, Any]] = {}   # model_id -> {kind, obj, file}
        self._refs: Dict[str, Dict[str, Any]] = {}     # obj_ref -> {model_id, kind, obj}
        self._counter = 0
        self._native_txn = None  # set when native transactions verified

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def connect(self) -> Dict[str, Any]:
        if self._disp is None:
            self._disp = ComDispatcher(self._config.startup_timeout_s,
                                       self._config.call_timeout_s)
            info = self._disp.start_and_attach(
                self._config.progid, self._config.attach_mode,
                self._config.pd_exe, self._config.visible)
            self._app = self._disp.call(lambda: self._disp._app)
            self._probe_native_transactions()
            return {"attached": True, **info, "attach_mode": self._config.attach_mode,
                    "native_transactions": bool(self._native_txn)}
        return self.server_info()

    def disconnect(self) -> None:
        if self._disp:
            # Drop every COM proxy on the apartment that created it.  Releasing
            # one from a thread that never initialised COM raises
            # CO_E_NOTINITIALIZED (0x800401f0) - observed as a fatal exception
            # during interpreter teardown once the dispatcher is gone.
            try:
                self._disp.call(self._release_references, timeout=30)
            except Exception:
                pass
            self._disp.shutdown(self._config.auto_quit)
            self._disp = None
            self._app = None

    def _release_references(self) -> None:
        """Drop tracked COM references.  Runs on the dispatcher thread."""
        self._models.clear()
        self._refs.clear()
        self._app = None

    def is_connected(self) -> bool:
        return self._disp is not None and self._app is not None

    def server_info(self) -> Dict[str, Any]:
        if not self.is_connected():
            return {"connected": False, "progid": self._config.progid}
        info = self._disp.call(lambda: {"version": _safe(self._app, "Version", "?"),
                                        "home": _safe(self._app, "HomeDirectory", ""),
                                        "models": _coll_count(self._app.Models)})
        return {"connected": True, "progid": self._config.progid,
                "attach_mode": self._config.attach_mode, **info,
                "native_transactions": bool(self._native_txn)}

    def _probe_native_transactions(self) -> None:
        def probe():
            try:
                self._app.BeginTransaction()
                self._app.EndTransaction()
                return True
            except Exception:
                return False
        try:
            self._native_txn = self._disp.call(probe, timeout=60)
        except Exception:
            self._native_txn = False

    # ------------------------------------------------------------------
    # low-level helpers (must run on dispatcher thread)
    # ------------------------------------------------------------------
    def _next_ref(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _register(self, model_id: str, kind: str, obj: Any) -> str:
        ref = self._next_ref(kind[0] if kind != "reference" else "r")
        self._refs[ref] = {"model_id": model_id, "kind": kind, "obj": obj}
        return ref

    def _get_ref(self, model_id: str, kind: str, ref: str) -> Any:
        entry = self._refs.get(ref)
        if entry is None or entry["model_id"] != model_id or entry["kind"] != kind:
            raise ObjectNotFoundError(kind, ref, model_id)
        return entry["obj"]

    def _meta_of(self, model_id: str) -> Dict[str, Any]:
        """Container/capability metadata of the model's kind."""
        entry = self._models.get(model_id)
        return MODEL_KINDS[entry["kind"]] if entry else MODEL_KINDS["PDM"]

    def _primary_key_ref(self, model_id: str, obj: Any) -> Any:
        """Return the key/identifier object the model treats as primary.

        PDM keeps an authoritative ``Primary`` flag on the key; CDM/LDM have
        no such flag - the entity points at its identifier through
        ``PrimaryIdentifier`` instead, so ownership has to be walked back.
        """
        meta = self._meta_of(model_id)
        owner = _plain_attr(obj, "Entity", None) or _plain_attr(obj, "Table", None)
        if owner is None:
            entry = self._models.get(model_id)
            if entry is not None:
                for t in _iter_coll(_safe(entry["obj"], meta["objects_collection"], None)):
                    for k in _iter_coll(_safe(t, meta["key_coll"], None)):
                        if _same_com_object(k, obj):
                            owner = t
                            break
                    if owner is not None:
                        break
        if owner is None:
            return None
        return _safe(owner, meta["pk_owner_prop"], None)

    def _resolve(self, model_id: str, kind: str, ident: str,
                 parent_obj: Any = None, coll_name: Optional[str] = None) -> Any:
        """Resolve an object by registered ref first, then by code/name.

        ``parent_obj``/``coll_name`` narrow the search to a parent collection
        (e.g. a table's Columns).  Without them, model-level collections are
        scanned; for columns/indexes this walks all tables as a last resort.
        """
        entry = self._refs.get(ident)
        if entry and entry["model_id"] == model_id and entry["kind"] == kind:
            return entry["obj"]
        if parent_obj is not None and coll_name:
            found = _find_in_coll(parent_obj, coll_name, ident)
            if found is not None:
                return found
        model_entry = self._models.get(model_id)
        if model_entry is not None:
            meta = MODEL_KINDS[model_entry["kind"]]
            root = {"table": meta["objects_collection"],
                    "reference": meta["ref_collection"],
                    "domain": "Domains",
                    "package": "Packages"}.get(kind)
            if root:
                found = _find_in_coll(model_entry["obj"], root, ident)
                if found is not None:
                    return found
            # columns/indexes are nested inside each table/entity; the
            # collection name differs per kind (Columns vs Attributes)
            nested = {"column": meta["column_coll"], "index": meta["index_coll"]}.get(kind)
            if nested:
                for t in _iter_coll(_safe(model_entry["obj"],
                                          meta["objects_collection"], None)):
                    found = _find_in_coll(t, nested, ident)
                    if found is not None:
                        return found
        raise ObjectNotFoundError(kind, ident, model_id)

    @staticmethod
    def _obj_ref_by_identity(model_id: str, obj: Any) -> Optional[str]:
        # inverse lookup helper
        return None

    # ------------------------------------------------------------------
    # object serialisation (dispatcher thread)
    # ------------------------------------------------------------------
    def _obj_dict(self, model_id: str, kind: str, obj: Any, brief: bool = False) -> Dict[str, Any]:
        ref = self._register(model_id, kind, obj)
        d: Dict[str, Any] = {
            "ref": ref,
            "name": _safe(obj, "Name", ""),
            "code": _safe(obj, "Code", ""),
            "comment": _safe(obj, "Comment", "") if not brief else "",
        }
        if kind == "column":
            d.update({
                "data_type": _safe(obj, "DataType", ""),
                "length": _safe(obj, "Length", None),
                "precision": _safe(obj, "Precision", None),
                "mandatory": bool(_safe(obj, "Mandatory", False)),
                "primary": bool(_safe(obj, "Primary", False)),
                "default_value": _safe(obj, "DefaultValue", None),
                "description": _safe(obj, "Description", ""),
                "domain": _safe(_safe(obj, "Domain", None), "Code", None),
            })
        if kind == "key":
            meta = self._meta_of(model_id)
            if meta["pk_flag_prop"]:
                d["primary"] = bool(_safe(obj, meta["pk_flag_prop"], False))
            else:
                d["primary"] = _same_com_object(self._primary_key_ref(model_id, obj), obj)
            d["columns"] = [self._obj_dict(model_id, "column", c, brief=True)
                            for c in _iter_coll(_safe(obj, meta["column_coll"], None))]
        if kind == "index":
            d["unique"] = bool(_safe(obj, "Unique", False))
            cols = []
            for ic in _iter_coll(_safe(obj, "IndexColumns", None)):
                col_obj = _safe(ic, "Column", None)
                if col_obj is not None:
                    cols.append(self._obj_dict(model_id, "column", col_obj, brief=True))
                else:
                    cols.append({"name": _safe(ic, "Name", ""), "expression": _safe(ic, "Expression", "")})
            d["columns"] = cols
        if kind == "reference":
            meta = self._meta_of(model_id)
            if meta["ref_style"] == "physical":
                parent = _safe(obj, "ParentTable", None)
                child = _safe(obj, "ChildTable", None)
                d.update({
                    "mandatory": bool(_safe(obj, "Mandatory", False)),
                    "parent_role": _safe(obj, "ParentRole", ""),
                    "child_role": _safe(obj, "ChildRole", ""),
                    "update_constraint": _safe(obj, "UpdateConstraint", ""),
                    "delete_constraint": _safe(obj, "DeleteConstraint", ""),
                })
            else:
                # conceptual (CDM/LDM): the ends are Entity1/Entity2 and the
                # multiplicities are per-direction role cardinalities whose
                # value syntax is "lo,hi" ("1,1" / "0,n"), not SQL "0..*"
                parent = _safe(obj, "Entity1", None)
                child = _safe(obj, "Entity2", None)
                child_card = _safe(obj, "Entity1ToEntity2RoleCardinality", "")
                parent_card = _safe(obj, "Entity2ToEntity1RoleCardinality", "")
                d.update({
                    "entity1": _safe(parent, "Code", None),
                    "entity2": _safe(child, "Code", None),
                    "parent_cardinality": parent_card,
                    "child_cardinality": child_card,
                    "cardinality": child_card,
                    "dependent_role": _safe(obj, "DependentRole", ""),
                    # role played by Entity1 when read from Entity2, mirroring
                    # the PDM Parent/ChildRole naming
                    "parent_role": _plain_attr(obj, "Entity2ToEntity1RoleName", ""),
                    "child_role": _plain_attr(obj, "Entity1ToEntity2RoleName", ""),
                    "mandatory": _conceptual_mandatory(parent_card),
                })
            d.update({
                "parent_table": _safe(parent, "Code", None),
                "child_table": _safe(child, "Code", None),
                "parent_table_ref": self._register(model_id, "table", parent) if parent else None,
                "child_table_ref": self._register(model_id, "table", child) if child else None,
                "joins": [],
            })
            for j in _iter_coll(_safe(obj, "Joins", None)):
                pc, cc = _safe(j, "ParentTableColumn", None), _safe(j, "ChildTableColumn", None)
                d["joins"].append({
                    "parent_column": _safe(pc, "Code", "") if pc else "",
                    "child_column": _safe(cc, "Code", "") if cc else "",
                })
        if kind == "table" and not brief:
            meta = self._meta_of(model_id)
            d["column_count"] = _coll_count(_safe(obj, meta["column_coll"], None))
            d["index_count"] = (_coll_count(_safe(obj, meta["index_coll"], None))
                                if meta["index_coll"] else 0)
            d["key_count"] = _coll_count(_safe(obj, meta["key_coll"], None))
        if kind == "domain":
            d.update({"data_type": _safe(obj, "DataType", ""),
                      "length": _safe(obj, "Length", None),
                      "precision": _safe(obj, "Precision", None)})
        if kind == "package":
            d["file_name"] = _safe(obj, "FileName", "")
        return d

    # ------------------------------------------------------------------
    # model management
    # ------------------------------------------------------------------
    def list_models(self) -> List[Dict[str, Any]]:
        def read():
            out = []
            for m in _iter_coll(self._app.Models):
                out.append(self._model_info_from_obj(m))
            return out
        return self._disp.call(read)

    def _model_info_from_obj(self, m: Any) -> Dict[str, Any]:
        kind = self._detect_kind(m)
        model_id = self._ensure_model_entry(m, kind)
        info = self._models[model_id]
        counts = {}
        coll_map = {"PDM": {"tables": "Tables", "references": "References",
                            "domains": "Domains", "packages": "Packages"},
                    "CDM": {"tables": "Entities", "references": "Relationships",
                            "domains": "Domains", "packages": "Packages"},
                    "LDM": {"tables": "Entities", "references": "Relationships",
                            "domains": "Domains", "packages": "Packages"}}[kind]
        for key, coll in coll_map.items():
            counts[key] = _coll_count(_safe(m, coll, None))
        # diagram health: content copied without its symbols opens on a blank
        # diagram, so surface both counts and let callers notice
        diagrams = _safe(m, MODEL_KINDS[kind]["diagram_collection"], None)
        counts["diagrams"] = _coll_count(diagrams)
        counts["symbols"] = 0
        if counts["diagrams"]:
            try:
                counts["symbols"] = _coll_count(_safe(diagrams.Item(0), "Symbols", None))
            except Exception:
                pass
        dbms = _safe(_safe(m, "DBMS", None), "Code", "") if kind == "PDM" else ""
        return {"model_id": model_id, "kind": kind,
                "name": _safe(m, "Name", ""), "code": _safe(m, "Code", ""),
                "comment": _safe(m, "Comment", ""),
                "file": info.get("file") or _model_file(m),
                "read_only": info.get("read_only", False),
                "dbms": dbms, "object_counts": counts}

    def _detect_kind(self, m: Any) -> str:
        """Classify an open model by its metaclass id.

        ClassKind arrives either as an int or as a numeric string depending on
        how the proxy was bound (live-verified: a natively generated LDM
        reported the string '1598421368'), so normalise before comparing.  The
        collection probes are only a last resort and must not go through
        ``_safe``, whose GetAttribute fallback answers even for properties the
        object does not have.
        """
        ck = _plain_attr(m, "ClassKind", None)
        try:
            ck_int = int(str(ck).strip())
        except Exception:
            ck_int = None
        if ck_int is not None:
            for kind, meta in MODEL_KINDS.items():
                if ck_int == meta["model_class"]:
                    return kind
        # last resort, most specific container first
        if _plain_attr(m, "Tables", None) is not None:
            return "PDM"
        if _plain_attr(m, "LogicalDiagrams", None) is not None:
            return "LDM"
        if _plain_attr(m, "ConceptualDiagrams", None) is not None:
            return "CDM"
        if _plain_attr(m, "Entities", None) is not None:
            return "CDM"
        return "PDM"

    def _ensure_model_entry(self, m: Any, kind: str) -> str:
        # pywin32 may return a fresh proxy per enumeration; identify models by
        # their stable ObjectID instead of Python object identity
        oid = str(_safe(m, "ObjectID", "") or "")
        if oid:
            for mid, entry in self._models.items():
                if entry.get("object_id") == oid:
                    entry["kind"] = kind
                    entry["obj"] = m  # refresh proxy
                    return mid
        model_id = self._next_ref("m")
        self._models[model_id] = {"obj": m, "kind": kind, "file": None,
                                  "read_only": False, "object_id": oid}
        return model_id

    def open_model(self, path: str, read_only: bool = False) -> Dict[str, Any]:
        p = Path(path)
        if not p.is_file():
            raise ObjectNotFoundError("File", path)
        suffix = p.suffix.lower()
        kind = {".pdm": "PDM", ".cdm": "CDM", ".ldm": "LDM"}.get(suffix)
        if kind is None:
            raise InvalidParamsError(f"Unsupported model file type '{suffix}' "
                                     "(supported: .pdm, .cdm, .ldm)")

        def do_open():
            m = self._app.OpenModel(str(p), bool(read_only))
            if m is None:
                raise OperationFailedError(f"PowerDesigner failed to open '{path}'")
            detected = self._detect_kind(m)
            # trust the API over the suffix; do NOT rebind `kind` here —
            # assigning inside this closure raised UnboundLocalError (live-verified)
            model_id = self._ensure_model_entry(m, detected)
            self._models[model_id]["file"] = str(p.resolve())
            self._models[model_id]["read_only"] = bool(read_only)
            return self._model_info_from_obj(m)
        return self._disp.call(do_open)

    def create_model(self, kind: str, name: str, code: str = "",
                     dbms: Optional[str] = None) -> Dict[str, Any]:
        kind = (kind or "").upper()
        if kind not in MODEL_KINDS:
            raise InvalidParamsError("kind must be one of PDM, CDM, LDM")
        meta = MODEL_KINDS[kind]

        def do_create():
            template = meta["diagram_template"]
            if kind == "PDM" and (dbms or self._config.default_dbms):
                template = f"|DBMS={dbms or self._config.default_dbms}" + template
            m = self._app.CreateModel(meta["model_class"], template, 0)
            if m is None:
                raise OperationFailedError(f"CreateModel({kind}) returned no model")
            _set_name_code(m, name, code or name)
            model_id = self._ensure_model_entry(m, kind)
            self._models[model_id]["file"] = None
            if kind == "PDM" and (dbms or self._config.default_dbms):
                try:
                    m.ChangeDBMS(dbms or self._config.default_dbms)
                except Exception:
                    pass  # DBMS stays default; reported via get_model_info
            return self._model_info_from_obj(m)
        return self._disp.call(do_create)

    def _model_entry(self, model_id: str) -> Dict[str, Any]:
        entry = self._models.get(model_id)
        if entry is None:
            raise ModelNotFoundError(model_id)
        return entry

    def save_model(self, model_id: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def do_save():
            m = entry["obj"]
            if not entry.get("file"):
                file_attr = _model_file(m)
                if file_attr:
                    entry["file"] = file_attr
            if not entry.get("file"):
                raise OperationFailedError(
                    "Model was never saved; use save_model_as to choose a target file")
            if not m.CanSave():
                raise OperationFailedError("Model reports CanSave()=False (read-only?)")
            m.Save()
            stray = _stray_saved_file(Path(entry["file"]))
            if stray is not None:
                log.warning("PowerDesigner wrote the model to %s instead of %s; "
                            "close and re-open the model to fix the binding",
                            stray, entry["file"])
            return {"model_id": model_id, "file": entry["file"], "saved": True}
        return self._disp.call(do_save)

    def save_model_as(self, model_id: str, path: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        kind = entry["kind"]
        ext = {"PDM": ".pdm", "CDM": ".cdm", "LDM": ".ldm"}[kind]
        target = Path(path)
        if target.suffix.lower() not in (".pdm", ".cdm", ".ldm"):
            target = target.with_name(target.name + ext)
        if target.exists():
            raise PdMcpError(ErrorCode.CONFLICT, f"Target file already exists: {target}")

        def do_save_as():
            m = entry["obj"]
            old_file = entry.get("file") or _model_file(m)
            if old_file:
                # save current state, close, copy, reopen at the new location
                m.Save()
                m.Close()
                import shutil
                shutil.copy2(old_file, target)
                reopened = self._app.OpenModel(str(target), False)
                mid = self._ensure_model_entry(reopened, kind)
                self._models[mid]["file"] = str(target)
                return {"model_id": mid, "file": str(target), "saved": True}
            # Never-saved model: PD has no SaveAs; verified workaround -
            # bind a fresh empty model to the target file via
            # CreateModel(kind, targetFile) using a ShellNew template copy,
            # then copy all content across and Save().
            import shutil
            home = _safe(self._app, "HomeDirectory", "") or ""
            tpl = Path(home) / "ShellNew" / {"PDM": "pdmodel16.pdm",
                                             "CDM": "pdmodel16.cdm",
                                             "LDM": "pdmodel16.ldm"}[kind]
            if not tpl.is_file():
                raise OperationFailedError(
                    f"ShellNew template not found: {tpl}; cannot bind a file to "
                    "an unsaved model. Save the model once in PowerDesigner and "
                    "retry.")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tpl, target)
            meta = MODEL_KINDS[kind]
            m2 = self._app.CreateModel(meta["model_class"], str(target), 0)
            if m2 is None:
                raise OperationFailedError(f"CreateModel from template failed for {target}")
            _set_name_code(m2, _safe(m, "Name", target.stem),
                           _safe(m, "Code", "") or target.stem)
            # capture the SOURCE object before swapping the tracked entry,
            # then swap so subsequently-created references land on the new model
            src_obj = m
            for ref in [r for r, e in self._refs.items() if e["model_id"] == model_id]:
                del self._refs[ref]
            entry["obj"] = m2
            counts = self._copy_content_sync(src_obj, kind, model_id, m2)
            # symbols attached programmatically land on top of each other
            laid_out = _auto_layout_first_diagram(m2)
            try:
                m2.Save()
            except Exception as exc:
                raise OperationFailedError(f"Saving the re-bound model failed: {exc}")
            # PD 16.5 normalises FileName on Save and writes the real content to
            # the bound path *without* its model extension, leaving the
            # requested file as the ShellNew stub - live-verified for .pdm,
            # .cdm and .ldm on a freshly bound model.  Promote the real file and
            # re-open it so the model stays bound to the requested path.
            # PD 16.5 writes the real content to the extension-less sibling and
            # leaves the requested file as the ShellNew stub, so the model has
            # to be closed before the file can be moved into place and re-opened.
            stray = _stray_saved_file(target)
            promoted = False
            if stray is not None:
                try:
                    m2.Close()
                except Exception as exc:
                    log.warning("could not close the bound model before moving "
                                "%s: %s", stray, exc)
                try:
                    stray.replace(target)
                    promoted = True
                except Exception as exc:
                    log.warning("could not move %s onto %s: %s", stray, target, exc)
            if promoted:
                try:
                    reopened = self._app.OpenModel(str(target), False)
                except Exception as exc:
                    reopened = None
                    log.warning("re-opening %s after the move failed: %s",
                                target, exc)
                if reopened is not None:
                    for ref in [r for r, e in self._refs.items()
                                if e["model_id"] == model_id]:
                        del self._refs[ref]
                    entry["obj"] = reopened
                else:
                    log.warning("%s holds the model but it is no longer open; "
                                "call open_model to continue using it", target)
            elif stray is not None:
                log.warning("the saved model is in %s, not in %s", stray, target)
            entry["file"] = str(target)
            if promoted and entry["obj"] is not src_obj:
                # the in-memory original is superseded by the verified file-bound
                # copy; leaving it open is what made PowerDesigner show two
                # models with the same name
                try:
                    src_obj.Close()
                except Exception as exc:
                    log.info("could not close the superseded in-memory model: %s", exc)
            return {"model_id": model_id, "file": str(target), "saved": True,
                    "copied": counts, "auto_layout": laid_out,
                    "promoted": promoted,
                    "note": "model content and diagram symbols copied onto a "
                            "file-bound model (PowerDesigner has no SaveAs for "
                            "unsaved models)"}
        return self._disp.call(do_save_as)

    def _copy_identifiers_sync(self, src: Any, dst: Any, kind: str) -> int:
        """Copy a CDM/LDM entity's identifiers and restore the primary pointer.

        Identifiers are keyed on the entity's ``PrimaryIdentifier`` rather than
        on a per-attribute flag, so the pointer has to be re-wired after the
        members have been added - and the target attributes only exist once
        :meth:`_create_column_on` has run for the entity.  Runs on the COM
        thread.
        """
        meta = MODEL_KINDS[kind]
        ident_cls = {"CDM": CDM_CLASSES["key"], "LDM": LDM_CLASSES["key"]}[kind]
        dst_coll = _safe(dst, meta["key_coll"], None)
        if dst_coll is None:
            return 0
        src_primary = _safe(src, meta["pk_owner_prop"], None)
        count = 0
        for ident in _iter_coll(_safe(src, meta["key_coll"], None)):
            new_ident = dst_coll.CreateNew(ident_cls)
            _set_name_code(new_ident, _safe(ident, "Name", ""), _safe(ident, "Code", ""))
            for member in _iter_coll(_safe(ident, meta["column_coll"], None)):
                target = _find_in_coll(dst, meta["column_coll"], _safe(member, "Code", ""))
                if target is not None:
                    try:
                        new_ident.Attributes.Add(target)
                    except Exception:
                        pass
            count += 1
            if src_primary is not None and _same_com_object(ident, src_primary):
                _try_set(dst, **{meta["pk_owner_prop"]: new_ident})
        return count

    def _copy_content_sync(self, src_obj: Any, kind: str, model_id: str,
                           dst_obj: Any) -> Dict[str, int]:
        """Copy the whole content of one model onto another of the same kind.

        Tables/entities carry their attributes and keys/identifiers (plus
        indexes in a PDM); references/relationships are rebuilt in a second
        pass by code so that the FK migration PowerDesigner performs on the
        child side cannot race the attribute copy.  Runs on the COM thread -
        must not call self._disp.call.

        PowerDesigner's content-level copy moves model objects only and does
        **not** carry diagram symbols, so every object created here is
        re-attached to the destination's default diagram afterwards; without
        that the re-bound model opens with a blank diagram (live-verified).
        """
        meta = MODEL_KINDS[kind]
        conceptual = meta["ref_style"] == "conceptual"
        counts = {"tables": 0, "columns": 0, "primary_keys": 0, "indexes": 0,
                  "references": 0}
        src_coll = _safe(src_obj, meta["objects_collection"], None)
        new_objects: List[Any] = []

        # pass 1: tables/entities + attributes + keys/identifiers + indexes
        for t in _iter_coll(src_coll):
            tcode = _safe(t, "Code", "")
            nt = dst_obj.CreateObject(meta["object_class"], "", -1, False)
            if nt is None:
                raise OperationFailedError(
                    f"CreateObject returned no object while copying "
                    f"{tcode or '(unnamed)'}")
            _set_name_code(nt, _safe(t, "Name", tcode), tcode)
            if _safe(t, "Comment", ""):
                _try_set(nt, Comment=_safe(t, "Comment", ""))
            new_tref = self._register(model_id, "table", nt)
            new_objects.append(nt)
            counts["tables"] += 1
            pk_codes = []
            for c in _iter_coll(_safe(t, meta["column_coll"], None)):
                spec = {"name": _safe(c, "Name", ""), "code": _safe(c, "Code", ""),
                        "data_type": _safe(c, "DataType", ""),
                        "length": _safe(c, "Length", None),
                        "precision": _safe(c, "Precision", None),
                        "mandatory": bool(_safe(c, "Mandatory", False)),
                        "default_value": _safe(c, "DefaultValue", None),
                        "comment": _safe(c, "Comment", ""),
                        "primary": bool(_safe(c, "Primary", False))}
                if spec["primary"]:
                    pk_codes.append(spec["code"])
                self._create_column_on(nt, model_id, spec)
                counts["columns"] += 1
            if conceptual:
                if self._copy_identifiers_sync(t, nt, kind):
                    counts["primary_keys"] += 1
            else:
                if pk_codes:
                    self._create_primary_key_sync(model_id, new_tref, pk_codes)
                    counts["primary_keys"] += 1
                for idx in _iter_coll(_safe(t, meta["index_coll"], None)):
                    idx_codes = []
                    for ic in _iter_coll(_safe(idx, "IndexColumns", None)):
                        col_obj = _safe(ic, "Column", None)
                        if col_obj is not None:
                            idx_codes.append(_safe(col_obj, "Code", ""))
                    if idx_codes:
                        self._create_index_sync(model_id, new_tref, idx_codes,
                                                name=_safe(idx, "Name", ""),
                                                unique=bool(_safe(idx, "Unique", False)))
                        counts["indexes"] += 1

        # pass 2: references/relationships (resolved by code)
        for r in _iter_coll(_safe(src_obj, meta["ref_collection"], None)):
            try:
                if conceptual:
                    e1 = _safe(r, "Entity1", None)
                    e2 = _safe(r, "Entity2", None)
                    if e1 is None or e2 is None:
                        continue
                    copied = self._create_relationship_sync(
                        model_id, _safe(e1, "Code", ""), _safe(e2, "Code", ""),
                        name=_safe(r, "Name", ""), code=_safe(r, "Code", ""),
                        comment=_safe(r, "Comment", ""),
                        cardinality=_safe(r, "Entity1ToEntity2RoleCardinality", None),
                        parent_cardinality=_safe(r, "Entity2ToEntity1RoleCardinality", None),
                        dependent_role=_safe(r, "DependentRole", None))
                else:
                    parent = _safe(r, "ParentTable", None)
                    child = _safe(r, "ChildTable", None)
                    if parent is None or child is None:
                        continue
                    # carrying code/name across keeps the FK constraint naming
                    # identical (PD would otherwise re-prefix it, FK_FK_xxx)
                    copied = self._create_reference_sync(
                        model_id, _safe(parent, "Code", ""), _safe(child, "Code", ""),
                        name=_safe(r, "Name", ""), code=_safe(r, "Code", ""),
                        comment=_safe(r, "Comment", ""),
                        cardinality=_cardinality_string(r), update_key=False)
                new_objects.append(self._refs[copied["ref"]]["obj"])
                counts["references"] += 1
            except Exception as exc:
                log.warning("copy: reference %s skipped: %s",
                            _safe(r, "Code", "?"), exc)
                continue

        # the content copy leaves no symbols behind - rebuild them
        for obj in new_objects:
            _attach_to_first_diagram(dst_obj, obj)
        count_attached = len(new_objects)
        log.info("copy: re-attached symbols for %d objects", count_attached)
        return counts

    def close_model(self, model_id: str, save: bool = False) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def do_close():
            m = entry["obj"]
            if save and entry.get("file"):
                try:
                    if m.CanSave():
                        m.Save()
                except Exception:
                    pass
            m.Close()
            # drop refs belonging to this model
            for ref in [r for r, e in self._refs.items() if e["model_id"] == model_id]:
                del self._refs[ref]
            del self._models[model_id]
            return {"model_id": model_id, "closed": True, "saved": bool(save)}
        return self._disp.call(do_close)

    def get_model_info(self, model_id: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        return self._disp.call(lambda: self._model_info_from_obj(entry["obj"]))

    # ------------------------------------------------------------------
    # packages
    # ------------------------------------------------------------------
    def list_packages(self, model_id: str) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)

        def read():
            return [self._obj_dict(model_id, "package", p)
                    for p in _iter_coll(_safe(entry["obj"], "Packages", None))]
        return self._disp.call(read)

    def get_package(self, model_id: str, package_ref: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def read():
            obj = self._resolve(model_id, "package", package_ref)
            return self._obj_dict(model_id, "package", obj)
        return self._disp.call(read)

    # ------------------------------------------------------------------
    # tables / entities
    # ------------------------------------------------------------------
    def _objects_collection(self, entry: Dict[str, Any]):
        kind = entry["kind"]
        return _safe(entry["obj"], "Tables" if kind == "PDM" else "Entities", None)

    def list_tables(self, model_id: str, package_ref: Optional[str] = None,
                    query: Optional[str] = None) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)

        def read():
            kind_word = "table"
            out = []
            if package_ref:
                pkg = self._resolve(model_id, "package", package_ref)
                coll = _safe(pkg, "Tables" if entry["kind"] == "PDM" else "Entities", None)
            else:
                coll = self._objects_collection(entry)
            for obj in _iter_coll(coll):
                d = self._obj_dict(model_id, kind_word, obj, brief=True)
                if query and query.lower() not in (d["code"] + d["name"]).lower():
                    continue
                out.append(d)
            return out
        return self._disp.call(read)

    def get_table(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        kind_word = "table"

        def read():
            obj = self._resolve(model_id, "table", table_ref)
            meta = self._meta_of(model_id)
            d = self._obj_dict(model_id, kind_word, obj)
            d["columns"] = [self._obj_dict(model_id, "column", c)
                            for c in _iter_coll(_safe(obj, meta["column_coll"], None))]
            d["keys"] = [self._obj_dict(model_id, "key", k)
                         for k in _iter_coll(_safe(obj, meta["key_coll"], None))]
            d["primary_key"] = next((k for k in d["keys"] if k.get("primary")), None)
            d["indexes"] = [self._obj_dict(model_id, "index", i)
                            for i in _iter_coll(_safe(obj, meta["index_coll"], None))]
            d["references_out"] = [self._obj_dict(model_id, "reference", r)
                                   for r in _iter_coll(_safe(obj, "OutReferences", None))] \
                if _safe(obj, "OutReferences", None) is not None else []
            d["references_in"] = [self._obj_dict(model_id, "reference", r)
                                  for r in _iter_coll(_safe(obj, "InReferences", None))] \
                if _safe(obj, "InReferences", None) is not None else []
            return d
        return self._disp.call(read)

    def create_table(self, model_id: str, name: str, code: str = "",
                     comment: str = "", columns: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        if not name:
            raise InvalidParamsError("Table name is required")
        kind_word = "table"
        cls = MODEL_KINDS[entry["kind"]]["object_class"]

        def do_create():
            m = entry["obj"]
            if not _coll_allows(m, "Tables" if entry["kind"] == "PDM" else "Entities"):
                raise OperationFailedError("Model does not allow adding objects here")
            obj = m.CreateObject(cls, "", -1, False)
            if obj is None:
                raise OperationFailedError("CreateObject returned no object "
                                           "(model read-only or invalid class)")
            _set_name_code(obj, name, code or name)
            if comment:
                _try_set(obj, Comment=comment)
            _attach_to_first_diagram(m, obj)
            ref = self._register(model_id, kind_word, obj)
            for spec in columns or []:
                self._create_column_on(obj, model_id, spec)
            return self._obj_dict(model_id, kind_word, obj)
        return self._disp.call(do_create)

    def update_table(self, model_id: str, table_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        kind_word = "table"

        def do_update():
            obj = self._resolve(model_id, "table", table_ref)
            name, code = updates.get("name"), updates.get("code")
            if name or code:
                _set_name_code(obj, name or _safe(obj, "Name", ""),
                               code or _safe(obj, "Code", ""))
            if updates.get("comment") is not None:
                obj.Comment = updates["comment"]
            return self._obj_dict(model_id, kind_word, obj)
        return self._disp.call(do_update)

    def delete_table(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        kind_word = "table"

        def do_delete():
            obj = self._resolve(model_id, "table", table_ref)
            code = _safe(obj, "Code", "")
            obj.delete()
            for ref in [r for r, e in self._refs.items()
                        if e["model_id"] == model_id and e["obj"] is obj]:
                del self._refs[ref]
            return {"deleted": kind_word, "ref": table_ref, "code": code}
        return self._disp.call(do_delete)

    # ------------------------------------------------------------------
    # columns
    # ------------------------------------------------------------------
    def _create_column_on(self, table_obj: Any, model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Add one attribute/column to a table or entity.

        The container is ``Columns`` in a PDM but ``Attributes`` in a
        CDM/LDM entity, and the data-type vocabulary differs too (DBMS syntax
        vs PowerDesigner's own).  ``primary`` is only meaningful in a PDM,
        where it is a flag on the column; in CDM/LDM primacy is expressed by
        the entity's primary identifier, which :meth:`create_primary_key`
        builds.
        """
        meta = self._meta_of(model_id)
        kind = self._models[model_id]["kind"]
        coll = _safe(table_obj, meta["column_coll"], None)
        if coll is None:
            raise OperationFailedError(
                f"Object has no {meta['column_coll']} collection")
        col = coll.CreateNew({"PDM": PDM_CLASSES["column"],
                              "CDM": CDM_CLASSES["column"],
                              "LDM": LDM_CLASSES["column"]}[kind])
        name = spec.get("name") or spec.get("code")
        code = spec.get("code") or name
        _set_name_code(col, name, code)
        if spec.get("default_value") is not None:
            _try_set(col, DefaultValue=spec["default_value"])
        # Length/Precision must ride on the DataType string; direct
        # Column.Length assignment is a silent no-op on PD 16.5 (live-verified)
        if meta["ref_style"] == "physical":
            full_dt = _compose_full_data_type(spec.get("data_type"),
                                              spec.get("length"), spec.get("precision"))
        else:
            # CDM/LDM hold PowerDesigner's own type vocabulary
            # ("Variable characters(20)"), not DBMS column syntax
            full_dt = conceptual_data_type(spec.get("data_type"),
                                           spec.get("length"), spec.get("precision"))
        if full_dt:
            _try_set(col, DataType=full_dt)
        if spec.get("mandatory") is not None:
            _try_set(col, Mandatory=bool(spec["mandatory"]))
        if spec.get("comment"):
            _try_set(col, Comment=str(spec["comment"]))
        if spec.get("description"):
            _try_set(col, Description=str(spec["description"]))
        if spec.get("domain"):
            dom = self._resolve_domain(model_id, spec["domain"])
            if dom is not None:
                _try_set(col, Domain=dom)
        if spec.get("primary") and meta["pk_flag_prop"]:
            _try_set(col, Primary=True)
        return self._obj_dict(model_id, "column", col)

    def list_columns(self, model_id: str, table_ref: str,
                     query: Optional[str] = None) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)
        coll_name = kind_meta(entry["kind"])["column_coll"]

        def read():
            obj = self._resolve(model_id, "table", table_ref)
            out = []
            for c in _iter_coll(_safe(obj, coll_name, None)):
                d = self._obj_dict(model_id, "column", c)
                if query and query.lower() not in (d["code"] + d["name"]).lower():
                    continue
                out.append(d)
            return out
        return self._disp.call(read)

    def _entry_obj_kind(self, entry: Dict[str, Any]) -> str:
        return "table"

    def get_column(self, model_id: str, table_ref: str, column_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def read():
            table = self._resolve(model_id, "table", table_ref)
            col = self._resolve(model_id, "column", column_ref, table,
                                self._meta_of(model_id)["column_coll"])
            return self._obj_dict(model_id, "column", col)
        return self._disp.call(read)

    def create_column(self, model_id: str, table_ref: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def do_create():
            obj = self._resolve(model_id, "table", table_ref)
            if not (spec.get("name") or spec.get("code")):
                raise InvalidParamsError("Column name is required")
            return self._create_column_on(obj, model_id, spec)
        return self._disp.call(do_create)

    def update_column(self, model_id: str, table_ref: str, column_ref: str,
                      updates: Dict[str, Any]) -> Dict[str, Any]:
        self._model_entry(model_id)

        def do_update():
            table = self._resolve(model_id, "table", table_ref)
            col = self._resolve(model_id, "column", column_ref, table,
                                self._meta_of(model_id)["column_coll"])
            name, code = updates.get("name"), updates.get("code")
            if name or code:
                _set_name_code(col, name or _safe(col, "Name", ""),
                               code or _safe(col, "Code", ""))
            kwargs: Dict[str, Any] = {}
            if updates.get("default_value") is not None:
                kwargs["DefaultValue"] = updates["default_value"]
            # Length/Precision must ride on the DataType string; direct
            # Column.Length assignment is a silent no-op on PD 16.5 (live-verified)
            full_dt = _compose_full_data_type(
                updates.get("data_type") or _safe(col, "DataType", ""),
                updates.get("length"), updates.get("precision"))
            if full_dt:
                kwargs["DataType"] = full_dt
            if updates.get("mandatory") is not None:
                kwargs["Mandatory"] = bool(updates["mandatory"])
            if updates.get("comment") is not None:
                kwargs["Comment"] = updates["comment"]
            if updates.get("description") is not None:
                kwargs["Description"] = updates["description"]
            if updates.get("primary") is not None:
                kwargs["Primary"] = bool(updates["primary"])
            if updates.get("domain"):
                dom = self._resolve_domain(model_id, updates["domain"])
                if dom is not None:
                    kwargs["Domain"] = dom
            _try_set(col, **kwargs)
            return self._obj_dict(model_id, "column", col)
        return self._disp.call(do_update)

    def delete_column(self, model_id: str, table_ref: str, column_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def do_delete():
            table = self._resolve(model_id, "table", table_ref)
            col = self._resolve(model_id, "column", column_ref, table,
                                self._meta_of(model_id)["column_coll"])
            code = _safe(col, "Code", "")
            col.delete()
            for ref in [r for r, e in self._refs.items()
                        if e["model_id"] == model_id and e["obj"] is col]:
                del self._refs[ref]
            return {"deleted": "column", "ref": column_ref, "code": code}
        return self._disp.call(do_delete)

    def _resolve_domain(self, model_id: str, domain_ref: str):
        try:
            return self._resolve(model_id, "domain", domain_ref)
        except ObjectNotFoundError:
            return None

    # ------------------------------------------------------------------
    # keys
    # ------------------------------------------------------------------
    def list_keys(self, model_id: str, table_ref: str) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)
        key_coll = kind_meta(entry["kind"])["key_coll"]

        def read():
            obj = self._resolve(model_id, "table", table_ref)
            return [self._obj_dict(model_id, "key", k)
                    for k in _iter_coll(_safe(obj, key_coll, None))]
        return self._disp.call(read)

    def create_primary_key(self, model_id: str, table_ref: str, columns: List[str],
                           name: str = "", code: str = "") -> Dict[str, Any]:
        if not columns:
            raise InvalidParamsError("Primary key needs at least one column")
        return self._disp.call(lambda: self._create_primary_key_sync(
            model_id, table_ref, columns, name, code))

    def _create_primary_key_sync(self, model_id: str, table_ref: str, columns: List[str],
                                 name: str = "", code: str = "") -> Dict[str, Any]:
        """Create the primary key (PDM) / primary identifier (CDM, LDM).

        PDM marks primacy with a flag on the key (and tolerates the
        vendor-sample style of flagging the columns directly).  CDM/LDM
        attributes carry no flag at all, so an ``Identifier`` must be created
        and the entity pointed at it through ``PrimaryIdentifier`` -
        live-verified as the only working route.
        """
        entry = self._model_entry(model_id)
        meta = kind_meta(entry["kind"])
        key_cls = {"PDM": PDM_CLASSES["key"], "CDM": CDM_CLASSES["key"],
                   "LDM": LDM_CLASSES["key"]}[entry["kind"]]
        if True:  # body runs on the COM thread
            obj = self._resolve(model_id, "table", table_ref)
            col_coll = meta["column_coll"]
            member_coll = "Columns" if meta["pk_flag_prop"] else "Attributes"
            # resolve column/attribute objects by code
            col_objs = []
            for want in columns:
                found = _find_in_coll(obj, col_coll, want)
                if found is None:
                    raise ObjectNotFoundError(col_coll[:-1], want, model_id)
                col_objs.append(found)
            if meta["pk_flag_prop"]:
                # clear existing PK membership and drop previous PK key objects
                for c in _iter_coll(_safe(obj, col_coll, None)):
                    if _safe(c, meta["pk_flag_prop"], False):
                        _try_set(c, **{meta["pk_flag_prop"]: False})
                for k in list(_iter_coll(_safe(obj, meta["key_coll"], None))):
                    if _safe(k, meta["pk_flag_prop"], False):
                        k.delete()
            else:
                # A CDM/LDM identifier cannot be updated in place, and a new one
                # cannot reuse the old name while it exists ("That name already
                # exists!", live-verified).  Deleting the superseded identifier
                # clears the entity's pointer by itself, whereas assigning None
                # to PrimaryIdentifier raises a type mismatch.
                prev = _plain_attr(obj, meta["pk_owner_prop"], None)
                if prev is not None:
                    try:
                        prev.delete()
                    except Exception as exc:
                        log.warning("could not drop the previous identifier on "
                                    "%s: %s", _safe(obj, "Code", "?"), exc)
            keys_coll = _safe(obj, meta["key_coll"], None)
            if keys_coll is None:
                raise OperationFailedError(
                    f"Object has no {meta['key_coll']} collection")
            # a PDM without an explicit name still has a usable PK via the
            # column flag, so keep the historical "no name -> no key object"
            # behaviour there; CDM/LDM *must* materialise the identifier
            key = None
            if name or code or not meta["pk_flag_prop"]:
                default_name = (f"PK_{_safe(obj, 'Code', '')}" if meta["pk_flag_prop"]
                                else f"ID_{_safe(obj, 'Code', '')}")
                key_name = name or code or default_name
                try:
                    key = keys_coll.CreateNew(key_cls)
                    _set_name_code(key, key_name, code or key_name)
                except Exception:
                    key = None
            if key is not None:
                for c in col_objs:
                    added = False
                    try:
                        getattr(key, member_coll).Add(c)
                        added = True
                    except Exception:
                        pass
                    if not added and meta["pk_flag_prop"]:
                        _try_set(c, Primary=True)
                if meta["pk_flag_prop"]:
                    _try_set(key, Primary=True)
                    return self._obj_dict(model_id, "key", key)
                _try_set(obj, **{meta["pk_owner_prop"]: key})
                if not _same_com_object(_plain_attr(obj, meta["pk_owner_prop"], None), key):
                    # fail loudly: a silently unregistered identifier is
                    # invisible until conversion stops migrating it
                    raise OperationFailedError(
                        f"PowerDesigner did not register the identifier on "
                        f"{_safe(obj, 'Code', '')}")
                d = self._obj_dict(model_id, "key", key)
                d["primary"] = True
                return d
            if not meta["pk_flag_prop"]:
                raise OperationFailedError(
                    f"Could not create an identifier on {_safe(obj, 'Code', '')}")
            # vendor-sample style: mark columns Primary directly
            for c in col_objs:
                _try_set(c, Primary=True)
            # report resulting PK via first column
            return {"primary_columns": columns, "table": _safe(obj, "Code", "")}

    def remove_primary_key(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def do_remove():
            meta = kind_meta(entry["kind"])
            obj = self._resolve(model_id, "table", table_ref)
            if meta["pk_flag_prop"]:
                for c in _iter_coll(_safe(obj, meta["column_coll"], None)):
                    if _safe(c, meta["pk_flag_prop"], False):
                        _try_set(c, **{meta["pk_flag_prop"]: False})
                for k in list(_iter_coll(_safe(obj, meta["key_coll"], None))):
                    if _safe(k, meta["pk_flag_prop"], False):
                        k.delete()
            else:
                prev = _safe(obj, meta["pk_owner_prop"], None)
                if prev is not None:
                    _try_set(obj, **{meta["pk_owner_prop"]: None})
                    try:
                        prev.delete()
                    except Exception:
                        pass
            return {"removed": "primary_key", "table": _safe(obj, "Code", "")}
        return self._disp.call(do_remove)

    # ------------------------------------------------------------------
    # references
    # ------------------------------------------------------------------
    def list_references(self, model_id: str) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)
        meta = kind_meta(entry["kind"])

        def read():
            # the registry kind must match what _resolve/_obj_dict expect,
            # otherwise refs handed out here cannot be used again
            return [self._obj_dict(model_id, "reference", r)
                    for r in _iter_coll(_safe(entry["obj"], meta["ref_collection"], None))]
        return self._disp.call(read)

    def get_reference(self, model_id: str, reference_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def read():
            obj = self._resolve(model_id, "reference", reference_ref)
            return self._obj_dict(model_id, "reference", obj)
        return self._disp.call(read)

    def create_reference(self, model_id: str, parent_table: str, child_table: str,
                         parent_columns: Optional[List[str]] = None,
                         child_columns: Optional[List[str]] = None,
                         name: str = "", code: str = "", comment: str = "",
                         cardinality: Optional[str] = None,
                         update_key: bool = True,
                         parent_cardinality: Optional[str] = None,
                         dependent_role: Optional[str] = None) -> Dict[str, Any]:
        return self._disp.call(lambda: self._create_reference_sync(
            model_id, parent_table, child_table, parent_columns, child_columns,
            name, code, comment, cardinality, update_key,
            parent_cardinality, dependent_role))

    def _create_reference_sync(self, model_id: str, parent_table: str, child_table: str,
                               parent_columns: Optional[List[str]] = None,
                               child_columns: Optional[List[str]] = None,
                               name: str = "", code: str = "", comment: str = "",
                               cardinality: Optional[str] = None,
                               update_key: bool = True,
                               parent_cardinality: Optional[str] = None,
                               dependent_role: Optional[str] = None) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        if uses_conceptual_relationships(entry["kind"]):
            # CDM/LDM describe an association, not a foreign key: columns and
            # FK migration have no meaning there, so the physical-only
            # arguments are ignored on purpose
            return self._create_relationship_sync(
                model_id, parent_table, child_table, name, code, comment,
                cardinality, parent_cardinality, dependent_role)
        ref_cls = PDM_CLASSES["reference"]
        join_cls = PDM_CLASSES["reference_join"]
        if True:  # body runs on the COM thread
            m = entry["obj"]
            parent = _find_in_coll(m, "Tables", parent_table)
            child = _find_in_coll(m, "Tables", child_table)
            if parent is None:
                raise ObjectNotFoundError("Table", parent_table, model_id)
            if child is None:
                raise ObjectNotFoundError("Table", child_table, model_id)
            ref = m.References.CreateNew(ref_cls)
            ref_name = name or f"FK_{_safe(child, 'Code', '')}_{_safe(parent, 'Code', '')}"
            _set_name_code(ref, ref_name, code or ref_name)
            # PD prepends its own "FK_" template prefix when this is left
            # empty, producing duplicated names like FK_FK_xxx (live-verified)
            _try_set(ref, ForeignKeyConstraintName=code or ref_name)
            if comment:
                _try_set(ref, Comment=comment)
            _try_set(ref, ParentTable=parent)
            _try_set(ref, ChildTable=child)

            if parent_columns and child_columns:
                if len(parent_columns) != len(child_columns):
                    raise InvalidParamsError("parent_columns and child_columns must have equal length")
                # PD auto-derives joins as soon as ChildTable is assigned;
                # clear them first, otherwise explicit joins are duplicated
                # (live-verified: two identical user_id->user_id joins)
                for auto_join in list(_iter_coll(_safe(ref, "Joins", None))):
                    try:
                        auto_join.Delete()
                    except Exception:
                        try:
                            _safe(ref, "Joins", None).Remove(auto_join)
                        except Exception:
                            pass
                for pc_code, cc_code in zip(parent_columns, child_columns):
                    pc = _find_in_coll(parent, "Columns", pc_code)
                    cc = _find_in_coll(child, "Columns", cc_code)
                    if pc is None or cc is None:
                        raise ObjectNotFoundError("Column", pc_code if pc is None else cc_code, model_id)
                    join = ref.Joins.CreateNew(join_cls)
                    _try_set(join, ParentTableColumn=pc, ChildTableColumn=cc)
            else:
                # derive from parent PK: assign ParentKey so PD generates joins;
                # verify and fall back to manual joins
                pk = None
                for k in _iter_coll(_safe(parent, "Keys", None)):
                    if _safe(k, "Primary", False):
                        pk = k
                        break
                if pk is not None:
                    try:
                        ref.ParentKey = pk
                    except Exception:
                        pass
                if _coll_count(_safe(ref, "Joins", None)) == 0 and pk is not None:
                    for pc in _iter_coll(_safe(pk, "Columns", None)):
                        pc_code = _safe(pc, "Code", "")
                        cc = _find_in_coll(child, "Columns", pc_code)
                        if cc is None:
                            if update_key:
                                cc = self._create_column_on(child, model_id, {
                                    "name": _safe(pc, "Name", pc_code), "code": pc_code,
                                    "data_type": _safe(pc, "DataType", ""),
                                    "length": _safe(pc, "Length", None),
                                    "precision": _safe(pc, "Precision", None),
                                    "comment": f"FK -> {_safe(parent, 'Code', '')}.{pc_code}"})
                                cc = self._refs[cc["ref"]]["obj"]
                            else:
                                continue
                        join = ref.Joins.CreateNew(join_cls)
                        _try_set(join, ParentTableColumn=pc, ChildTableColumn=cc)

            if cardinality:
                try:
                    lo, hi = str(cardinality).split(",") if "," in str(cardinality) else ("0", "n")
                    _try_set(ref, MinimumCardinality=lo.strip(), MaximumCardinality=hi.strip())
                except Exception:
                    pass
            _attach_to_first_diagram(m, ref)
            return self._obj_dict(model_id, "reference", ref)

    def _create_relationship_sync(self, model_id: str, entity1: str, entity2: str,
                                  name: str = "", code: str = "", comment: str = "",
                                  cardinality: Optional[str] = None,
                                  parent_cardinality: Optional[str] = None,
                                  dependent_role: Optional[str] = None) -> Dict[str, Any]:
        """Create a CDM/LDM relationship, Entity1 -> Entity2.

        PowerDesigner stores conceptual multiplicity as a per-direction role
        cardinality in ``"lo,hi"`` form (``"1,1"``, ``"0,n"``, ``"1,n"``) -
        *not* SQL's ``0..*`` spelling; this was live-verified by dissecting the
        saved ``.cdm`` XML, whose element names map 1:1 onto the automation
        properties.  Following the PDM convention, ``cardinality`` describes
        the multiplicity at the Entity2 (child) end - how many Entity2
        instances one Entity1 may have - while ``parent_cardinality``
        describes the Entity1 end and defaults to ``1,1``.  Using
        ``parent_cardinality="0,n"`` on both ends therefore expresses M:N.
        """
        entry = self._model_entry(model_id)
        meta = kind_meta(entry["kind"])
        ref_cls = {"CDM": CDM_CLASSES["reference"],
                   "LDM": LDM_CLASSES["reference"]}[entry["kind"]]
        if True:  # body runs on the COM thread
            m = entry["obj"]
            e1 = _find_in_coll(m, meta["objects_collection"], entity1)
            e2 = _find_in_coll(m, meta["objects_collection"], entity2)
            if e1 is None:
                raise ObjectNotFoundError("Entity", entity1, model_id)
            if e2 is None:
                raise ObjectNotFoundError("Entity", entity2, model_id)
            ref = m.Relationships.CreateNew(ref_cls)
            ref_name = name or code or f"rel_{_safe(e1, 'Code', '')}_{_safe(e2, 'Code', '')}"
            _set_name_code(ref, ref_name, code or ref_name)
            if comment:
                _try_set(ref, Comment=comment)
            _try_set(ref, Entity1=e1)
            _try_set(ref, Entity2=e2)
            child_card = _normalize_cardinality(cardinality) or "0,n"
            parent_card = _normalize_cardinality(parent_cardinality) or "1,1"
            _try_set(ref, Entity1ToEntity2RoleCardinality=child_card)
            _try_set(ref, Entity2ToEntity1RoleCardinality=parent_card)
            side = _resolve_dependent_side(dependent_role, e1, e2)
            if side:
                _try_set(ref, DependentRole=side)
            _attach_to_first_diagram(m, ref)
            d = self._obj_dict(model_id, "reference", ref)
            d["cardinality"] = child_card
            d["parent_cardinality"] = parent_card
            return d

    def update_reference(self, model_id: str, reference_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def do_update():
            meta = kind_meta(entry["kind"])
            ref = self._resolve(model_id, "reference", reference_ref)
            name, code = updates.get("name"), updates.get("code")
            if name or code:
                _set_name_code(ref, name or _safe(ref, "Name", ""),
                               code or _safe(ref, "Code", ""))
            kwargs: Dict[str, Any] = {}
            if updates.get("comment") is not None:
                kwargs["Comment"] = updates["comment"]
            if meta["ref_style"] == "physical":
                if updates.get("mandatory") is not None:
                    kwargs["Mandatory"] = bool(updates["mandatory"])
                if updates.get("parent_role") is not None:
                    kwargs["ParentRole"] = updates["parent_role"]
                if updates.get("child_role") is not None:
                    kwargs["ChildRole"] = updates["child_role"]
                if updates.get("cardinality"):
                    try:
                        lo, hi = str(updates["cardinality"]).split(",")
                        kwargs["MinimumCardinality"] = lo.strip()
                        kwargs["MaximumCardinality"] = hi.strip()
                    except Exception:
                        pass
            else:
                if updates.get("parent_role") is not None:
                    kwargs["Entity2ToEntity1RoleName"] = updates["parent_role"]
                if updates.get("child_role") is not None:
                    kwargs["Entity1ToEntity2RoleName"] = updates["child_role"]
                card = _normalize_cardinality(updates.get("cardinality"))
                if card:
                    kwargs["Entity1ToEntity2RoleCardinality"] = card
                card = _normalize_cardinality(updates.get("parent_cardinality"))
                if card:
                    kwargs["Entity2ToEntity1RoleCardinality"] = card
                side = _resolve_dependent_side(
                    updates.get("dependent_role"),
                    _safe(ref, "Entity1", None), _safe(ref, "Entity2", None))
                if side:
                    kwargs["DependentRole"] = side
            _try_set(ref, **kwargs)
            return self._obj_dict(model_id, "reference", ref)
        return self._disp.call(do_update)

    def delete_reference(self, model_id: str, reference_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def do_delete():
            ref = self._resolve(model_id, "reference", reference_ref)
            code = _safe(ref, "Code", "")
            ref.delete()
            for r in [r for r, e in self._refs.items()
                      if e["model_id"] == model_id and e["obj"] is ref]:
                del self._refs[r]
            return {"deleted": "reference", "ref": reference_ref, "code": code}
        return self._disp.call(do_delete)

    # ------------------------------------------------------------------
    # indexes
    # ------------------------------------------------------------------
    def list_indexes(self, model_id: str, table_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)
        meta = kind_meta(entry["kind"])

        def read():
            out = []
            if not meta["index_coll"]:
                return out
            if table_ref:
                obj = self._resolve(model_id, "table", table_ref)
                for i in _iter_coll(_safe(obj, meta["index_coll"], None)):
                    out.append(self._obj_dict(model_id, "index", i))
            else:
                for t in _iter_coll(_safe(entry["obj"], meta["objects_collection"], None)):
                    for i in _iter_coll(_safe(t, meta["index_coll"], None)):
                        out.append(self._obj_dict(model_id, "index", i, brief=True))
            return out
        return self._disp.call(read)

    def _require_index_support(self, model_id: str) -> Dict[str, Any]:
        """Indexes are a physical concept; guard CDM/LDM with a clear error.

        Raising here (rather than letting the COM call fail with an opaque
        AttributeError) is what keeps the mock and the real backend honest
        about the same capability boundary.
        """
        meta = self._meta_of(model_id)
        if not meta["index_coll"]:
            kind = self._models.get(model_id, {}).get("kind", "?")
            raise InvalidParamsError(
                f"Indexes do not exist in a {kind} model - they are a physical "
                "(PDM) concept. Convert to a PDM first, or use keys/identifiers.")
        return meta

    def get_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def read():
            meta = self._require_index_support(model_id)
            table = self._resolve(model_id, "table", table_ref)
            idx = self._resolve(model_id, "index", index_ref, table, meta["index_coll"])
            return self._obj_dict(model_id, "index", idx)
        return self._disp.call(read)

    def create_index(self, model_id: str, table_ref: str, columns: List[str],
                     name: str = "", code: str = "", unique: bool = False,
                     comment: str = "") -> Dict[str, Any]:
        self._model_entry(model_id)
        self._require_index_support(model_id)
        if not columns:
            raise InvalidParamsError("Index needs at least one column")
        return self._disp.call(lambda: self._create_index_sync(
            model_id, table_ref, columns, name, code, unique, comment))

    def _create_index_sync(self, model_id: str, table_ref: str, columns: List[str],
                           name: str = "", code: str = "", unique: bool = False,
                           comment: str = "") -> Dict[str, Any]:
        idx_cls = PDM_CLASSES["index"]
        idx_col_cls = PDM_CLASSES["index_column"]
        if True:  # body runs on the COM thread
            meta = self._meta_of(model_id)
            table = self._resolve(model_id, "table", table_ref)
            idx = table.Indexes.CreateNew(idx_cls)
            idx_name = name or ("idx_" + "_".join(columns))
            _set_name_code(idx, idx_name, code or idx_name)
            if unique:
                _try_set(idx, Unique=True)
            if comment:
                _try_set(idx, Comment=comment)
            for col_code in columns:
                col = _find_in_coll(table, meta["column_coll"], col_code)
                if col is None:
                    raise ObjectNotFoundError(meta["column_coll"][:-1], col_code, model_id)
                ic = idx.IndexColumns.CreateNew(idx_col_cls)
                _try_set(ic, Column=col)
            return self._obj_dict(model_id, "index", idx)

    def update_index(self, model_id: str, table_ref: str, index_ref: str,
                     updates: Dict[str, Any]) -> Dict[str, Any]:
        self._model_entry(model_id)
        self._require_index_support(model_id)

        def do_update():
            meta = self._meta_of(model_id)
            table = self._resolve(model_id, "table", table_ref)
            idx = self._resolve(model_id, "index", index_ref, table, meta["index_coll"])
            name, code = updates.get("name"), updates.get("code")
            if name or code:
                _set_name_code(idx, name or _safe(idx, "Name", ""),
                               code or _safe(idx, "Code", ""))
            kwargs: Dict[str, Any] = {}
            if updates.get("unique") is not None:
                kwargs["Unique"] = bool(updates["unique"])
            if updates.get("comment") is not None:
                kwargs["Comment"] = updates["comment"]
            _try_set(idx, **kwargs)
            if updates.get("columns"):
                table = self._resolve(model_id, "table", table_ref)
                # rebuild index columns
                try:
                    while _coll_count(_safe(idx, "IndexColumns", None)) > 0:
                        ic = idx.IndexColumns.Item(0)
                        ic.delete()
                except Exception:
                    pass
                for col_code in updates["columns"]:
                    col = _find_in_coll(table, meta["column_coll"], col_code)
                    ic = idx.IndexColumns.CreateNew(PDM_CLASSES["index_column"])
                    _try_set(ic, Column=col)
            return self._obj_dict(model_id, "index", idx)
        return self._disp.call(do_update)

    def delete_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)
        self._require_index_support(model_id)

        def do_delete():
            idx = self._resolve(model_id, "index", index_ref)
            code = _safe(idx, "Code", "")
            idx.delete()
            for r in [r for r, e in self._refs.items()
                      if e["model_id"] == model_id and e["obj"] is idx]:
                del self._refs[r]
            return {"deleted": "index", "ref": index_ref, "code": code}
        return self._disp.call(do_delete)

    # ------------------------------------------------------------------
    # domains
    # ------------------------------------------------------------------
    def list_domains(self, model_id: str) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)

        def read():
            return [self._obj_dict(model_id, "domain", d)
                    for d in _iter_coll(_safe(entry["obj"], "Domains", None))]
        return self._disp.call(read)

    def get_domain(self, model_id: str, domain_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def read():
            dom = self._get_ref(model_id, "domain", domain_ref)
            return self._obj_dict(model_id, "domain", dom)
        return self._disp.call(read)

    def create_domain(self, model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        dom_cls = {"PDM": PD_PDM_DOMAIN, "CDM": PD_CDM_DOMAIN, "LDM": PD_LDM_DOMAIN}[entry["kind"]]

        def do_create():
            m = entry["obj"]
            dom = m.Domains.CreateNew(dom_cls)
            name = spec.get("name") or spec.get("code")
            _set_name_code(dom, name, spec.get("code") or name)
            kwargs: Dict[str, Any] = {}
            if spec.get("data_type") is not None:
                kwargs["DataType"] = spec["data_type"]
            for api in ("Length", "Precision"):
                if spec.get(api.lower()) is not None:
                    kwargs[api] = spec[api.lower()]
            if spec.get("comment"):
                kwargs["Comment"] = spec["comment"]
            if spec.get("default_value") is not None:
                kwargs["DefaultValue"] = spec["default_value"]
            if spec.get("mandatory") is not None:
                kwargs["Mandatory"] = bool(spec["mandatory"])
            _try_set(dom, **kwargs)
            return self._obj_dict(model_id, "domain", dom)
        return self._disp.call(do_create)

    # ------------------------------------------------------------------
    # native check / ddl / conversion
    # ------------------------------------------------------------------
    def native_check_model(self, model_id: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def run():
            result = entry["obj"].CheckModel()
            messages = []
            try:
                for item in _iter_coll(result):
                    messages.append({
                        "severity": str(_safe(item, "Severity", "")),
                        "location": str(_safe(item, "LocationText", "") or _safe(item, "Location", "")),
                        "description": str(_safe(item, "DescriptionText", "") or _safe(item, "Description", "") or item),
                    })
            except Exception:
                messages = [{"severity": "?", "location": "", "description": str(result)}]
            errors = [m for m in messages if "error" in str(m["severity"]).lower()]
            warnings = [m for m in messages if "warning" in str(m["severity"]).lower()]
            return {"model_id": model_id, "engine": "powerdesigner-native",
                    "messages": messages, "errors": errors, "warnings": warnings,
                    "summary": {"errors": len(errors), "warnings": len(warnings)}}
        return self._disp.call(run)

    def generate_database(self, model_id: str, output_path: str,
                          options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        if entry["kind"] != "PDM":
            raise InvalidParamsError("generate_database requires a PDM model")
        options = options or {}
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        def run():
            import pythoncom
            m = entry["obj"]
            dbms = _safe(_safe(m, "DBMS", None), "Code", "") or _safe(_safe(m, "DBMS", None), "Name", "")
            asked = options.get("dbms")
            if asked and dbms and asked.lower() not in (dbms.lower(),):
                raise PdMcpError(
                    ErrorCode.INVALID_PARAMS,
                    f"Model DBMS is '{dbms}'; requested '{asked}'. Use ChangeDBMS "
                    "(create_model with dbms) or request the current DBMS.")
            # Documented PD 16.5 flow: set package options, then call the no-arg
            # GenerateDatabase through raw IDispatch (pywin32's dynamic binding
            # cannot construct the optional object parameters' defaults).
            opts = _safe(m, "GetPackageOptions", None)
            opts = opts() if callable(opts) else None
            if opts is not None:
                _try_set(opts, GenerateODBC=False, OneFile=True,
                         GenerationPathName=str(target.parent) + "\\",
                         GenerationScriptName=target.name)
            invoked = False
            try:
                ole = m._oleobj_
                dispid = ole.GetIDsOfNames(0, "GenerateDatabase")
                ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, False)
                invoked = True
            except Exception:
                try:
                    m.GenerateDatabase()
                    invoked = True
                except Exception:
                    pass
            if not target.exists():
                raise OperationFailedError(
                    "GenerateDatabase did not produce the expected SQL file "
                    f"({target}). invocation={invoked}, dbms='{dbms}'. "
                    "Check PowerDesigner's Result List for generation errors.")
            sql = target.read_text(encoding="utf-8", errors="replace")
            return {"model_id": model_id, "file": str(target),
                    "bytes": target.stat().st_size, "dbms": dbms,
                    "engine": "powerdesigner-native", "sql": sql}
        return self._disp.call(run)

    def convert_model(self, model_id: str, target_kind: str,
                      dbms: Optional[str] = None) -> Dict[str, Any]:
        from ..services.conversion import convert_model as fallback_convert
        entry = self._model_entry(model_id)
        src_kind = entry["kind"]
        target_kind = (target_kind or "").upper()
        pair = (src_kind, target_kind)
        if pair not in (("CDM", "LDM"), ("CDM", "PDM"), ("LDM", "PDM")):
            raise InvalidParamsError(
                f"Conversion {src_kind}->{target_kind} not supported "
                "(CDM->LDM, CDM->PDM, LDM->PDM)")

        # 1) try native generation.  PD 16.5 has no GeneratePhysicalDataModel /
        #    GenerateLogicalDataModel member at all (live-verified); the generic
        #    GenerateModel(ObjectSelection, Kind, Target, SaveDependencies) is the
        #    only route that applies PowerDesigner's own mapping rules - including
        #    the identifier->FK migration a course design depends on.
        #
        #    Classification and registration happen *inside* the COM call:
        #    inspecting the freshly generated model from another thread made
        #    _detect_kind() report PDM for a native LDM (live-verified), because
        #    a marshalled proxy answers differently from an in-apartment object.
        def try_native():
            m = entry["obj"]
            target_class = MODEL_KINDS[target_kind]["model_class"]
            new_m = None
            try:
                raw = _generate_model_via_invoke(m, target_class, "")
                if raw is not None:
                    from win32com.client import dynamic
                    new_m = dynamic.Dispatch(raw)
            except Exception as exc:
                log.info("native GenerateModel(%s) failed, falling back: %s",
                         target_kind, exc)
            if new_m is None:
                for method in ("GeneratePhysicalDataModel", "GenerateLogicalDataModel"):
                    want_phys = target_kind == "PDM"
                    if (method == "GeneratePhysicalDataModel") != want_phys:
                        continue
                    fn = getattr(m, method, None)
                    if fn is None:
                        continue
                    for args in ((), (dbms or "",) if dbms else ()):
                        try:
                            candidate = fn(*args) if args else fn()
                        except Exception:
                            continue
                        if candidate is not None:
                            new_m = candidate
                            break
                    if new_m is not None:
                        break
            if new_m is None:
                return None
            kind = self._detect_kind(new_m)
            self._ensure_model_entry(new_m, kind)
            return {"kind": kind, "info": self._model_info_from_obj(new_m)}

        native = self._disp.call(try_native)
        if native is not None:
            return {"source_model_id": model_id, "target_kind": native["kind"],
                    "engine": "powerdesigner-native",
                    "target_model": native["info"]}

        # 2) structured fallback conversion via adapter-level mapping
        result = fallback_convert(self, model_id, target_kind, dbms)
        result["engine"] = "mcp-structured"
        return result

    # ------------------------------------------------------------------
    # tracked object utilities (transactions)
    # ------------------------------------------------------------------
    def delete_object_by_ref(self, model_id: str, kind: str, obj_ref: str) -> Dict[str, Any]:
        if kind == "table":
            return self.delete_table(model_id, obj_ref)
        if kind == "column":
            return self.delete_column(model_id, "", obj_ref)
        if kind == "reference":
            return self.delete_reference(model_id, obj_ref)
        if kind == "index":
            return self.delete_index(model_id, "", obj_ref)
        if kind == "domain":
            def do_delete():
                dom = self._get_ref(model_id, "domain", obj_ref)
                code = _safe(dom, "Code", "")
                dom.delete()
                return {"deleted": "domain", "ref": obj_ref, "code": code}
            return self._disp.call(do_delete)
        raise InvalidParamsError(f"Cannot delete object kind '{kind}'")

    def update_object_props(self, model_id: str, kind: str, obj_ref: str,
                            props: Dict[str, Any]) -> Dict[str, Any]:
        if kind == "table":
            return self.update_table(model_id, obj_ref, props)
        if kind == "reference":
            return self.update_reference(model_id, obj_ref, props)
        raise InvalidParamsError(f"Cannot update object kind '{kind}' via generic props")

    def get_object_props(self, model_id: str, kind: str, obj_ref: str) -> Dict[str, Any]:
        if kind == "table":
            entry = self._model_entry(model_id)

            def read():
                obj = self._get_ref(model_id, "table", obj_ref)
                return {"name": _safe(obj, "Name", ""), "code": _safe(obj, "Code", ""),
                        "comment": _safe(obj, "Comment", "")}
            return self._disp.call(read)
        if kind == "reference":
            def read():
                obj = self._get_ref(model_id, "reference", obj_ref)
                return {"name": _safe(obj, "Name", ""), "code": _safe(obj, "Code", ""),
                        "comment": _safe(obj, "Comment", "")}
            return self._disp.call(read)
        raise InvalidParamsError(f"Cannot read object kind '{kind}' via generic props")


# ---------------------------------------------------------------------------
# module-level helpers (executed on the COM thread)
# ---------------------------------------------------------------------------

def _safe(obj: Any, attr: str, default: Any = None) -> Any:
    if obj is None:
        return default
    try:
        value = getattr(obj, attr)
        return default if value is None else value
    except Exception:
        try:
            return obj.GetAttribute(attr)
        except Exception:
            return default


def _try_set(obj: Any, **kwargs: Any) -> None:
    for attr, value in kwargs.items():
        try:
            setattr(obj, attr, value)
        except Exception:
            try:
                obj.SetAttribute(attr, value)
            except Exception:
                log.warning("_try_set: could not set %s on %s (property "
                            "unavailable or rejected)", attr,
                            _safe(obj, "Name", obj))


def _set_name_code(obj: Any, name: str, code: str) -> None:
    try:
        obj.SetNameAndCode(name, code)
        # SetNameAndCode may re-derive the code from naming conventions
        if code and _safe(obj, "Code", "") != code:
            obj.Code = code
    except Exception:
        try:
            obj.Name = name
        except Exception:
            pass
        try:
            obj.Code = code
        except Exception:
            pass


def _base_type_name(data_type: Any) -> str:
    """'VARCHAR(50)' -> 'VARCHAR' (strip parenthesised length/precision)."""
    dt = str(data_type or "").strip()
    i = dt.find("(")
    return (dt[:i].strip() if i > 0 else dt).upper()


def _compose_full_data_type(base: Any, length: Any, precision: Any) -> Optional[str]:
    """Carry length/precision inside the DataType string.

    Verified live on PD 16.5.0.3982 COM: assigning ``Column.Length`` /
    ``Column.Precision`` directly is a *silent no-op* (no exception raised,
    value stays 0) — lengths must be set via e.g. ``DataType='VARCHAR(50)'``
    or ``DataType='DECIMAL(10,2)'``.
    """
    base = _base_type_name(base)
    if not base:
        return None

    def _int(v: Any) -> int:
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    ln, pc = _int(length), _int(precision)
    if ln > 0 and pc > 0:
        return f"{base}({ln},{pc})"
    if ln > 0:
        return f"{base}({ln})"
    return base


def _coll_count(coll: Any) -> int:
    try:
        return int(coll.Count)
    except Exception:
        return 0


def _iter_coll(coll: Any):
    if coll is None:
        return
    try:
        n = int(coll.Count)
    except Exception:
        return
    for i in range(n):
        try:
            yield coll.Item(i)
        except Exception:
            continue


def _coll_allows(obj: Any, coll_name: str) -> bool:
    try:
        coll = getattr(obj, coll_name)
        return bool(coll.CanCreate()) if hasattr(coll, "CanCreate") else True
    except Exception:
        return False


def _find_in_coll(obj: Any, coll_name: str, code_or_name: str) -> Any:
    for item in _iter_coll(_safe(obj, coll_name, None)):
        if _safe(item, "Code", "") == code_or_name or _safe(item, "Name", "") == code_or_name:
            return item
    return None


def _model_file(m: Any) -> Optional[str]:
    value = _safe(m, "FileName", None)
    if value:
        return str(value)
    try:
        value = m.GetAttribute("FileName")
        return str(value) if value else None
    except Exception:
        return None


def _stray_saved_file(target: Path) -> Optional[Path]:
    """Path of the extension-less file PowerDesigner wrote instead of *target*.

    PD 16.5 normalises ``FileName`` on Save and writes the model to the bound
    path with its model extension stripped, leaving the requested file as the
    ShellNew stub (live-verified for .pdm, .cdm and .ldm: a 50 KB model landed
    in ``a_model`` beside a 1.9 KB ``a_model.pdm``).  Detection only - the
    caller has to close the model before the file can be moved, since PD keeps
    a handle on it.
    """
    stray = target.with_suffix("")
    try:
        if stray.is_file() and target.is_file() and \
                stray.stat().st_size > target.stat().st_size:
            return stray
    except Exception as exc:
        log.warning("could not inspect %s: %s", stray, exc)
    return None


def _generate_model_via_invoke(model_obj: Any, model_class: int,
                               target: str = "") -> Any:
    """``GenerateModel(ObjectSelection, Kind, Target, SaveDependencies)``.

    The signature comes from the PD 16.5 typelib (dispid 33554819).  pywin32
    cannot marshal the optional arguments of this member or of its wrappers
    ("The Python instance can not be converted to a COM object"), so the call
    goes through the raw IDispatch - the same pattern as GenerateDatabase and
    AttachLinkObject.  Returns a raw IDispatch pointer (dispatch it with
    ``win32com.client.dynamic.Dispatch``).
    """
    import pythoncom
    ole = model_obj._oleobj_
    return ole.InvokeTypes(33554819, 0, pythoncom.DISPATCH_METHOD, (9, 2),
                           ((9, 49), (3, 49), (8, 49), (11, 49)),
                           None, model_class, target, True)


def _attach_link_via_invoke(diagram: Any, obj: Any) -> bool:
    """Diagram.AttachLinkObject via raw IDispatch.

    Link-type objects (Reference/...) must be attached with AttachLinkObject;
    plain AttachObject is a *silent no-op* for them on PD 16.5 (live-verified).
    pywin32 dynamic binding cannot construct AttachLinkObject's optional
    parameters ("The Python instance can not be converted to a COM object"),
    so the call goes through the raw IDispatch - same pattern as
    GenerateDatabase.  Returns False when the call is not applicable.
    """
    try:
        import pythoncom
        ole = diagram._oleobj_
        dispid = ole.GetIDsOfNames(0, "AttachLinkObject")
        ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, False, obj._oleobj_)
        return True
    except Exception:
        return False


def _attach_to_first_diagram(model_obj: Any, obj: Any, link: Optional[bool] = None) -> None:
    """Attach a symbol so the object is visible in the default diagram.

    ``link`` selects the AttachLinkObject route; when left unset it is inferred
    from the object shape.  A CDM relationship has no ``Joins`` collection, so
    inferring on ``Joins`` alone would send it down the silent AttachObject
    path and drop the relationship line from the diagram.
    """
    if link is None:
        link = any(_has_attr(obj, attr) for attr in ("Joins", "Entity1", "Entity2"))
    for attr in ("PhysicalDiagrams", "ConceptualDiagrams", "LogicalDiagrams"):
        diagrams = _safe(model_obj, attr, None)
        if diagrams is not None:
            try:
                diagram = diagrams.Item(0)
                attached = False
                if link:
                    attached = _attach_link_via_invoke(diagram, obj)
                if not attached:
                    diagram.AttachObject(obj)
            except Exception:
                pass
            return


def _auto_layout_first_diagram(model_obj: Any) -> bool:
    """Best-effort AutoLayout on the default diagram.

    Symbols attached programmatically land on top of each other; AutoLayout is
    called through the raw IDispatch because pywin32 cannot build its optional
    arguments - the same limitation as GenerateDatabase/AttachLinkObject.
    """
    for attr in ("PhysicalDiagrams", "ConceptualDiagrams", "LogicalDiagrams"):
        diagrams = _safe(model_obj, attr, None)
        if diagrams is None:
            continue
        try:
            import pythoncom
            diagram = diagrams.Item(0)
            ole = diagram._oleobj_
            dispid = ole.GetIDsOfNames(0, "AutoLayout")
            ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, False)
            return True
        except Exception:
            return False
    return False


def _plain_attr(obj: Any, attr: str, default: Any = None) -> Any:
    """``getattr`` without :func:`_safe`'s GetAttribute fallback.

    GetAttribute answers for properties that do not exist, so ``_safe`` cannot
    be used to test whether a collection is really there - live-verified: an
    LDM and a CDM both came back as "PDM" because ``_safe(m, "Tables")``
    returned a value on models that have no Tables collection at all.
    """
    if obj is None:
        return default
    try:
        value = getattr(obj, attr, None)
        return default if value is None else value
    except Exception:
        return default


def _object_identity(obj: Any) -> Optional[str]:
    """Stable identity of a COM object, as a string.

    pywin32 hands back a fresh proxy on every enumeration, so ``is`` is
    useless.  ObjectID is the documented identity but its *type* varies by
    metaclass - models report an integer while CDM identifiers report a GUID
    string (live-verified) - hence the string form.
    """
    if obj is None:
        return None
    value = _plain_attr(obj, "ObjectID", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _same_com_object(a: Any, b: Any) -> bool:
    ia, ib = _object_identity(a), _object_identity(b)
    if ia is None or ib is None:
        return a is b
    return ia == ib


def _has_attr(obj: Any, attr: str) -> bool:
    """True when the property exists *and* holds a value."""
    return _plain_attr(obj, attr, None) is not None


# SQL-style multiplicities mapped onto PowerDesigner's conceptual "lo,hi"
# spelling (the only form the CDM/LDM role-cardinality properties accept)
_CARDINALITY_ALIASES = {
    "*": "0,n", "n": "0,n", "0..*": "0,n", "0..n": "0,n", "0,n": "0,n",
    "1..*": "1,n", "1..n": "1,n", "1,n": "1,n",
    "0..1": "0,1", "0,1": "0,1",
    "1": "1,1", "1..1": "1,1", "1,1": "1,1",
}


def _normalize_cardinality(value: Any) -> Optional[str]:
    """Normalise a multiplicity to PowerDesigner's conceptual "lo,hi" form."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in _CARDINALITY_ALIASES:
        return _CARDINALITY_ALIASES[text]
    if "," in text:
        return text
    if ".." in text:
        lo, _, hi = text.partition("..")
        hi = {"*": "n"}.get(hi.strip(), hi.strip())
        return f"{lo.strip()},{hi}"
    if text.isdigit():
        return text
    return None


def _resolve_dependent_side(value: Any, entity1: Any, entity2: Any) -> Optional[str]:
    """Map a user-facing dependent side onto PD's DependentRole ("A"/"B").

    Accepts "A"/"B" directly, "1"/"2" for Entity1/Entity2, or the code/name of
    one of the two entities.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.upper() in ("A", "B"):
        return text.upper()
    if text in ("1", "2"):
        return "A" if text == "1" else "B"
    for side, ent in (("A", entity1), ("B", entity2)):
        if ent is not None and text in (_safe(ent, "Code", ""), _safe(ent, "Name", "")):
            return side
    return None


def _conceptual_mandatory(parent_cardinality: Any) -> bool:
    """True when the Entity1 end is "1,x": each Entity2 needs exactly one Entity1.

    That is the conceptual equivalent of a NOT NULL foreign key in the PDM the
    model converts into.
    """
    return str(parent_cardinality or "").strip().startswith("1")


def _cardinality_string(ref: Any) -> Optional[str]:
    """Recompose a PDM reference cardinality from its min/max components."""
    lo = _safe(ref, "MinimumCardinality", None)
    hi = _safe(ref, "MaximumCardinality", None)
    if lo is None and hi is None:
        return None
    return f"{lo if lo is not None else 0},{hi if hi is not None else 'n'}"
