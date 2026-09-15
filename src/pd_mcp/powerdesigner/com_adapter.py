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
from .constants import MODEL_KINDS, PDM_CLASSES, CDM_CLASSES, LDM_CLASSES, PD_CDM_DOMAIN, PD_LDM_DOMAIN, PD_PDM_DOMAIN

_PROGID_DEFAULT = "PowerDesigner.Application"


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
        if not should_quit:
            return
        try:
            self.call(self._close_app, timeout=60)
        except Exception:
            pass

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
            self._disp.shutdown(self._config.auto_quit)
            self._disp = None
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
            root = {"table": MODEL_KINDS[model_entry["kind"]]["objects_collection"],
                    "reference": MODEL_KINDS[model_entry["kind"]]["ref_collection"],
                    "domain": "Domains",
                    "package": "Packages"}.get(kind)
            if root:
                found = _find_in_coll(model_entry["obj"], root, ident)
                if found is not None:
                    return found
            if kind == "column":
                for t in _iter_coll(_safe(model_entry["obj"],
                                          MODEL_KINDS[model_entry["kind"]]["objects_collection"], None)):
                    found = _find_in_coll(t, "Columns", ident)
                    if found is not None:
                        return found
            if kind == "index":
                for t in _iter_coll(_safe(model_entry["obj"],
                                          MODEL_KINDS[model_entry["kind"]]["objects_collection"], None)):
                    found = _find_in_coll(t, "Indexes", ident)
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
            d["primary"] = bool(_safe(obj, "Primary", False))
            d["columns"] = [self._obj_dict(model_id, "column", c, brief=True)
                            for c in _iter_coll(_safe(obj, "Columns", None))]
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
            parent = _safe(obj, "ParentTable", None)
            child = _safe(obj, "ChildTable", None)
            d.update({
                "parent_table": _safe(parent, "Code", None),
                "child_table": _safe(child, "Code", None),
                "parent_table_ref": self._register(model_id, "table", parent) if parent else None,
                "child_table_ref": self._register(model_id, "table", child) if child else None,
                "mandatory": bool(_safe(obj, "Mandatory", False)),
                "parent_role": _safe(obj, "ParentRole", ""),
                "child_role": _safe(obj, "ChildRole", ""),
                "update_constraint": _safe(obj, "UpdateConstraint", ""),
                "delete_constraint": _safe(obj, "DeleteConstraint", ""),
                "joins": [],
            })
            for j in _iter_coll(_safe(obj, "Joins", None)):
                pc, cc = _safe(j, "ParentTableColumn", None), _safe(j, "ChildTableColumn", None)
                d["joins"].append({
                    "parent_column": _safe(pc, "Code", "") if pc else "",
                    "child_column": _safe(cc, "Code", "") if cc else "",
                })
        if kind == "table" and not brief:
            d["column_count"] = _coll_count(_safe(obj, "Columns", None))
            d["index_count"] = _coll_count(_safe(obj, "Indexes", None))
            d["key_count"] = _coll_count(_safe(obj, "Keys", None))
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
        dbms = _safe(_safe(m, "DBMS", None), "Code", "") if kind == "PDM" else ""
        return {"model_id": model_id, "kind": kind,
                "name": _safe(m, "Name", ""), "code": _safe(m, "Code", ""),
                "comment": _safe(m, "Comment", ""),
                "file": info.get("file") or _model_file(m),
                "read_only": info.get("read_only", False),
                "dbms": dbms, "object_counts": counts}

    def _detect_kind(self, m: Any) -> str:
        ck = _safe(m, "ClassKind", None)
        if isinstance(ck, int):
            from .constants import PD_CDM_MODEL, PD_LDM_MODEL
            if ck == MODEL_KINDS["PDM"]["model_class"]:
                return "PDM"
            if ck == PD_CDM_MODEL:
                return "CDM"
            if ck == PD_LDM_MODEL:
                return "LDM"
        if _safe(m, "Tables", None) is not None:
            return "PDM"
        if _safe(m, "Entities", None) is not None:
            return "LDM"  # refined below via file suffix when known
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
            if detected != kind:
                kind = detected  # trust the API over the suffix
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
            try:
                m2.Save()
            except Exception as exc:
                raise OperationFailedError(f"Saving the re-bound model failed: {exc}")
            entry["file"] = str(target)
            return {"model_id": model_id, "file": str(target), "saved": True,
                    "copied": counts,
                    "note": "model content copied onto a file-bound model "
                            "(PowerDesigner has no SaveAs for unsaved models)"}
        return self._disp.call(do_save_as)

    def _copy_content_sync(self, src_obj: Any, kind: str, model_id: str,
                           dst_obj: Any) -> Dict[str, int]:
        """Copy all tables/columns/PKs/indexes/references between two open
        models.  Runs on the COM thread - must not call self._disp.call."""
        meta = MODEL_KINDS[kind]
        counts = {"tables": 0, "columns": 0, "primary_keys": 0, "indexes": 0,
                  "references": 0}
        src_coll = _safe(src_obj, meta["objects_collection"], None)
        col_classes = {"PDM": PDM_CLASSES["column"], "CDM": CDM_CLASSES["column"],
                       "LDM": LDM_CLASSES["column"]}

        # pass 1: tables + columns + PK + indexes
        for t in _iter_coll(src_coll):
            tcode = _safe(t, "Code", "")
            nt = dst_obj.CreateObject(meta["object_class"], "", -1, False)
            _set_name_code(nt, _safe(t, "Name", tcode), tcode)
            if _safe(t, "Comment", ""):
                _try_set(nt, Comment=_safe(t, "Comment", ""))
            new_tref = self._register(model_id, "table", nt)
            counts["tables"] += 1
            pk_codes = []
            for c in _iter_coll(_safe(t, "Columns", None)):
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
            if pk_codes:
                self._create_primary_key_sync(model_id, new_tref, pk_codes)
                counts["primary_keys"] += 1
            for idx in _iter_coll(_safe(t, "Indexes", None)):
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
        # pass 2: references (by codes)
        for r in _iter_coll(_safe(src_obj, meta["ref_collection"], None)):
            parent = _safe(r, "ParentTable", None)
            child = _safe(r, "ChildTable", None)
            if parent is None or child is None:
                continue
            try:
                self._create_reference_sync(
                    model_id, _safe(parent, "Code", ""), _safe(child, "Code", ""),
                    name=_safe(r, "Name", ""), comment=_safe(r, "Comment", ""),
                    update_key=False)
                counts["references"] += 1
            except Exception:
                continue
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
            d = self._obj_dict(model_id, kind_word, obj)
            d["columns"] = [self._obj_dict(model_id, "column", c)
                            for c in _iter_coll(_safe(obj, "Columns", None))]
            d["keys"] = [self._obj_dict(model_id, "key", k)
                         for k in _iter_coll(_safe(obj, "Keys", None))]
            d["primary_key"] = next((k for k in d["keys"] if k.get("primary")), None)
            d["indexes"] = [self._obj_dict(model_id, "index", i)
                            for i in _iter_coll(_safe(obj, "Indexes", None))]
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
        cls = PDM_CLASSES["column"] if _safe(table_obj, "Columns", None) is not None and \
            _safe(self._models[model_id]["obj"], "Tables", None) is not None else None
        # column class is the same numeric id family per kind; resolve via model kind
        kind = self._models[model_id]["kind"]
        from .constants import PD_CDM_ENTITY_ATTRIBUTE, PD_LDM_ENTITY_ATTRIBUTE
        cls = {"PDM": PDM_CLASSES["column"], "CDM": PD_CDM_ENTITY_ATTRIBUTE,
               "LDM": PD_LDM_ENTITY_ATTRIBUTE}[kind]
        coll = _safe(table_obj, "Columns", None)
        if coll is None:
            raise OperationFailedError("Object has no Columns collection")
        col = coll.CreateNew(cls)
        name = spec.get("name") or spec.get("code")
        code = spec.get("code") or name
        _set_name_code(col, name, code)
        if spec.get("default_value") is not None:
            _try_set(col, DefaultValue=spec["default_value"])
        # Length/Precision must ride on the DataType string; direct
        # Column.Length assignment is a silent no-op on PD 16.5 (live-verified)
        full_dt = _compose_full_data_type(spec.get("data_type"),
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
        if spec.get("primary"):
            _try_set(col, Primary=True)
        return self._obj_dict(model_id, "column", col)

    def list_columns(self, model_id: str, table_ref: str,
                     query: Optional[str] = None) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)

        def read():
            obj = self._resolve(model_id, "table", table_ref)
            out = []
            for c in _iter_coll(_safe(obj, "Columns", None)):
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
            col = self._resolve(model_id, "column", column_ref, table, "Columns")
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
            col = self._resolve(model_id, "column", column_ref, table, "Columns")
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
            col = self._resolve(model_id, "column", column_ref, table, "Columns")
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

        def read():
            obj = self._resolve(model_id, "table", table_ref)
            return [self._obj_dict(model_id, "key", k)
                    for k in _iter_coll(_safe(obj, "Keys", None))]
        return self._disp.call(read)

    def create_primary_key(self, model_id: str, table_ref: str, columns: List[str],
                           name: str = "", code: str = "") -> Dict[str, Any]:
        if not columns:
            raise InvalidParamsError("Primary key needs at least one column")
        return self._disp.call(lambda: self._create_primary_key_sync(
            model_id, table_ref, columns, name, code))

    def _create_primary_key_sync(self, model_id: str, table_ref: str, columns: List[str],
                                 name: str = "", code: str = "") -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        key_cls = {"PDM": PDM_CLASSES["key"]}.get(entry["kind"], PDM_CLASSES["key"])
        if True:  # body runs on the COM thread
            obj = self._resolve(model_id, "table", table_ref)
            # resolve column objects by code
            col_objs = []
            for want in columns:
                found = None
                for c in _iter_coll(_safe(obj, "Columns", None)):
                    if _safe(c, "Code", "") == want or _safe(c, "Name", "") == want:
                        found = c
                        break
                if found is None:
                    raise ObjectNotFoundError("Column", want, model_id)
                col_objs.append(found)
            # clear existing PK membership
            for c in _iter_coll(_safe(obj, "Columns", None)):
                if _safe(c, "Primary", False):
                    _try_set(c, Primary=False)
            # remove previous PK key objects
            for k in list(_iter_coll(_safe(obj, "Keys", None))):
                if _safe(k, "Primary", False):
                    k.delete()
            key = None
            keys_coll = _safe(obj, "Keys", None)
            if name or code:
                try:
                    key = keys_coll.CreateNew(key_cls)
                    _set_name_code(key, name or f"PK_{_safe(obj, 'Code', '')}",
                                   code or name or f"PK_{_safe(obj, 'Code', '')}")
                except Exception:
                    key = None
            if key is not None:
                for c in col_objs:
                    added = False
                    try:
                        key.Columns.Add(c)
                        added = True
                    except Exception:
                        pass
                    if not added:
                        _try_set(c, Primary=True)
                _try_set(key, Primary=True)
            else:
                # vendor-sample style: mark columns Primary directly
                for c in col_objs:
                    _try_set(c, Primary=True)
            if key is not None:
                return self._obj_dict(model_id, "key", key)
            # report resulting PK via first column
            return {"primary_columns": columns, "table": _safe(obj, "Code", "")}

    def remove_primary_key(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        entry = self._model_entry(model_id)

        def do_remove():
            obj = self._resolve(model_id, "table", table_ref)
            for c in _iter_coll(_safe(obj, "Columns", None)):
                if _safe(c, "Primary", False):
                    _try_set(c, Primary=False)
            for k in list(_iter_coll(_safe(obj, "Keys", None))):
                if _safe(k, "Primary", False):
                    k.delete()
            return {"removed": "primary_key", "table": _safe(obj, "Code", "")}
        return self._disp.call(do_remove)

    # ------------------------------------------------------------------
    # references
    # ------------------------------------------------------------------
    def list_references(self, model_id: str) -> List[Dict[str, Any]]:
        entry = self._model_entry(model_id)
        coll_name = "References" if entry["kind"] == "PDM" else "Relationships"

        def read():
            kind_word = "reference" if entry["kind"] == "PDM" else "relationship"
            return [self._obj_dict(model_id, kind_word, r)
                    for r in _iter_coll(_safe(entry["obj"], coll_name, None))]
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
                         update_key: bool = True) -> Dict[str, Any]:
        return self._disp.call(lambda: self._create_reference_sync(
            model_id, parent_table, child_table, parent_columns, child_columns,
            name, code, comment, cardinality, update_key))

    def _create_reference_sync(self, model_id: str, parent_table: str, child_table: str,
                               parent_columns: Optional[List[str]] = None,
                               child_columns: Optional[List[str]] = None,
                               name: str = "", code: str = "", comment: str = "",
                               cardinality: Optional[str] = None,
                               update_key: bool = True) -> Dict[str, Any]:
        entry = self._model_entry(model_id)
        if entry["kind"] != "PDM":
            raise InvalidParamsError("create_reference applies to PDM models; "
                                     "use list_relationships for CDM/LDM models")
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

    def update_reference(self, model_id: str, reference_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        self._model_entry(model_id)

        def do_update():
            ref = self._resolve(model_id, "reference", reference_ref)
            name, code = updates.get("name"), updates.get("code")
            if name or code:
                _set_name_code(ref, name or _safe(ref, "Name", ""),
                               code or _safe(ref, "Code", ""))
            kwargs: Dict[str, Any] = {}
            if updates.get("comment") is not None:
                kwargs["Comment"] = updates["comment"]
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

        def read():
            out = []
            if table_ref:
                obj = self._resolve(model_id, "table", table_ref)
                for i in _iter_coll(_safe(obj, "Indexes", None)):
                    out.append(self._obj_dict(model_id, "index", i))
            else:
                for t in _iter_coll(_safe(entry["obj"], "Tables", None)):
                    for i in _iter_coll(_safe(t, "Indexes", None)):
                        out.append(self._obj_dict(model_id, "index", i, brief=True))
            return out
        return self._disp.call(read)

    def get_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

        def read():
            table = self._resolve(model_id, "table", table_ref)
            idx = self._resolve(model_id, "index", index_ref, table, "Indexes")
            return self._obj_dict(model_id, "index", idx)
        return self._disp.call(read)

    def create_index(self, model_id: str, table_ref: str, columns: List[str],
                     name: str = "", code: str = "", unique: bool = False,
                     comment: str = "") -> Dict[str, Any]:
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
            table = self._resolve(model_id, "table", table_ref)
            idx = table.Indexes.CreateNew(idx_cls)
            idx_name = name or ("idx_" + "_".join(columns))
            _set_name_code(idx, idx_name, code or idx_name)
            if unique:
                _try_set(idx, Unique=True)
            if comment:
                _try_set(idx, Comment=comment)
            for col_code in columns:
                col = _find_in_coll(table, "Columns", col_code)
                if col is None:
                    raise ObjectNotFoundError("Column", col_code, model_id)
                ic = idx.IndexColumns.CreateNew(idx_col_cls)
                _try_set(ic, Column=col)
            return self._obj_dict(model_id, "index", idx)

    def update_index(self, model_id: str, table_ref: str, index_ref: str,
                     updates: Dict[str, Any]) -> Dict[str, Any]:
        self._model_entry(model_id)

        def do_update():
            table = self._resolve(model_id, "table", table_ref)
            idx = self._resolve(model_id, "index", index_ref, table, "Indexes")
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
                    col = _find_in_coll(table, "Columns", col_code)
                    ic = idx.IndexColumns.CreateNew(PDM_CLASSES["index_column"])
                    _try_set(ic, Column=col)
            return self._obj_dict(model_id, "index", idx)
        return self._disp.call(do_update)

    def delete_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        self._model_entry(model_id)

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

        # 1) try native generation (GeneratePhysicalDataModel / GenerateLogicalDataModel)
        def try_native():
            m = entry["obj"]
            for method in ("GeneratePhysicalDataModel", "GenerateLogicalDataModel"):
                want_phys = target_kind == "PDM"
                if (method == "GeneratePhysicalDataModel") != want_phys:
                    continue
                fn = getattr(m, method, None)
                if fn is None:
                    continue
                for args in ((), (dbms or "",) if dbms else ()):
                    try:
                        new_m = fn(*args) if args else fn()
                        if new_m is not None:
                            return new_m
                    except Exception:
                        continue
            return None

        new_m = self._disp.call(try_native)
        if new_m is not None:
            kind = self._detect_kind(new_m)
            mid = self._ensure_model_entry(new_m, kind)
            info = self._disp.call(lambda: self._model_info_from_obj(new_m))
            return {"source_model_id": model_id, "target_kind": kind,
                    "engine": "powerdesigner-native", "target_model": info}

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
    log = logging.getLogger(__name__)
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


def _attach_to_first_diagram(model_obj: Any, obj: Any) -> None:
    """Attach a symbol so the object is visible in the default diagram."""
    for attr in ("PhysicalDiagrams", "ConceptualDiagrams", "LogicalDiagrams"):
        diagrams = _safe(model_obj, attr, None)
        if diagrams is not None:
            try:
                diagram = diagrams.Item(0)
                attached = False
                if hasattr(obj, "Joins"):
                    attached = _attach_link_via_invoke(diagram, obj)
                if not attached:
                    diagram.AttachObject(obj)
            except Exception:
                pass
            return
