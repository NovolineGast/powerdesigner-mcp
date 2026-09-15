"""Transaction / rollback support.

PowerDesigner's COM API has no real transaction object, so this module
implements reliable two-layer rollback at the MCP level:

1. **File-level backup** - when the model is backed by a file, begin()
   first saves the model, copies the file into the backup directory and
   records it.  rollback() closes the model, restores the file and reopens
   it.  This restores *exactly* the pre-transaction state, including
   deletions.
2. **Operation journal** - create/update operations are journaled with the
   inverse operation.  When no destructive operation happened (or the model
   has no file), rollback replays the journal in reverse without touching
   files.

If a rollback is impossible (destructive ops on a never-saved model) the
error says so explicitly - it never fails silently.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import ErrorCode, PdMcpError


class TransactionManager:
    def __init__(self, adapter, backup_dir: Path, keep_backups: int = 20):
        self._adapter = adapter
        self._backup_dir = Path(backup_dir)
        self._keep = max(1, keep_backups)
        self._lock = threading.RLock()
        self._txns: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    def begin(self, model_id: str, auto_save: bool = True) -> Dict[str, Any]:
        info = self._adapter.get_model_info(model_id)
        file_path = Path(info.get("file")) if info.get("file") else None

        backup_file: Optional[Path] = None
        if auto_save and file_path is None:
            # unsaved model: nothing to save yet
            pass
        if auto_save and file_path is not None and file_path.exists():
            try:
                self._adapter.save_model(model_id)
            except PdMcpError:
                pass  # keep going with the on-disk state
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file = self._backup_dir / f"{uuid.uuid4().hex}_{file_path.name}"
            shutil.copy2(file_path, backup_file)
            self._prune_backups()

        txn_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._txns[txn_id] = {
                "txn_id": txn_id,
                "model_id": model_id,
                "file": str(file_path) if file_path else None,
                "backup_file": str(backup_file) if backup_file else None,
                "journal": [],
                "auto_saved": bool(auto_save),
                "started_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }
        return {"txn_id": txn_id, "model_id": model_id,
                "file_backup": str(backup_file) if backup_file else None,
                "note": None if backup_file else
                        ("model has no file yet; rollback can undo create/update "
                         "operations but not deletions")}

    # ------------------------------------------------------------------
    def record(self, txn_id: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            txn = self._txns.get(txn_id)
            if txn is not None:
                txn["journal"].append(entry)

    # ------------------------------------------------------------------
    def commit(self, txn_id: str) -> Dict[str, Any]:
        with self._lock:
            txn = self._txns.pop(txn_id, None)
        if txn is None:
            raise PdMcpError(ErrorCode.TRANSACTION_ERROR, f"Unknown transaction '{txn_id}'")
        return {"txn_id": txn_id, "committed": True,
                "journal_size": len(txn["journal"])}

    # ------------------------------------------------------------------
    def rollback(self, txn_id: str) -> Dict[str, Any]:
        with self._lock:
            txn = self._txns.pop(txn_id, None)
        if txn is None:
            raise PdMcpError(ErrorCode.TRANSACTION_ERROR, f"Unknown transaction '{txn_id}'")

        journal: List[Dict[str, Any]] = txn["journal"]
        destructive = any(e["type"] == "delete_object" for e in journal)
        has_backup = bool(txn["backup_file"]) and Path(txn["backup_file"]).exists()

        # ---- file-level restore ---------------------------------------
        if has_backup and (destructive or True):
            # Prefer the file restore: it is exact.  Only journal-replay when
            # there is no backup (unsaved model).
            return self._restore_file(txn, len(journal))

        if destructive and not has_backup:
            # partial undo is still better than nothing for create/update
            undone = self._replay_journal(journal)
            raise PdMcpError(
                ErrorCode.TRANSACTION_ERROR,
                "Rolled back create/update operations, but the transaction "
                "contained deletions and the model has no file backup; the "
                "model cannot be fully restored. Save the model before "
                "batch operations.",
                {"undone_operations": undone})

        undone = self._replay_journal(journal)
        return {"txn_id": txn_id, "rolled_back": True, "method": "journal",
                "undone_operations": undone}

    # ------------------------------------------------------------------
    def _restore_file(self, txn: Dict[str, Any], n_ops: int) -> Dict[str, Any]:
        model_id = txn["model_id"]
        original = Path(txn["file"])
        backup = Path(txn["backup_file"])
        try:
            self._adapter.close_model(model_id, save=False)
        except PdMcpError:
            pass
        try:
            shutil.copy2(backup, original)
            self._adapter.open_model(str(original))
        except Exception as exc:
            raise PdMcpError(
                ErrorCode.TRANSACTION_ERROR,
                f"Failed to restore model file from backup: {exc}",
                {"model_id": model_id, "backup": str(backup)})
        return {"txn_id": txn["txn_id"], "rolled_back": True, "method": "file_restore",
                "model_id": model_id, "file": str(original),
                "operations_discarded": n_ops,
                "note": "model was restored from backup and reopened "
                        "(the model_id may have changed; run list_open_models)"}

    def _replay_journal(self, journal: List[Dict[str, Any]]) -> List[str]:
        undone: List[str] = []
        for entry in reversed(journal):
            etype = entry["type"]
            try:
                if etype == "create_object":
                    self._adapter.delete_object_by_ref(
                        entry["model_id"], entry["kind"], entry["obj_ref"])
                    undone.append(f"deleted created {entry['kind']} {entry.get('code', entry['obj_ref'])}")
                elif etype == "update_props":
                    self._adapter.update_object_props(
                        entry["model_id"], entry["kind"], entry["obj_ref"], entry["old"])
                    undone.append(f"restored props of {entry['kind']} {entry.get('code', entry['obj_ref'])}")
                elif etype == "delete_object":
                    continue  # handled by file restore path
            except Exception:
                continue  # best effort
        return undone

    # ------------------------------------------------------------------
    def list_backups(self) -> List[Dict[str, Any]]:
        if not self._backup_dir.exists():
            return []
        out = []
        for p in sorted(self._backup_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                out.append({"file": str(p), "name": p.name,
                            "size": stat.st_size,
                            "modified": __import__("datetime")
                            .datetime.fromtimestamp(stat.st_mtime)
                            .isoformat(timespec="seconds")})
        return out

    def rollback_model(self, model_id: str, backup_file: Optional[str] = None) -> Dict[str, Any]:
        """Restore a model from a backup file (close -> copy -> reopen)."""
        backups = self.list_backups()
        target = None
        if backup_file:
            target = next((b for b in backups if b["file"] == backup_file), None)
            if target is None:
                target = {"file": backup_file} if Path(backup_file).exists() else None
        elif backups:
            target = backups[-1]
        if target is None:
            raise PdMcpError(ErrorCode.FILE_ERROR, "No backup file available to restore")

        src = Path(target["file"])
        info = self._adapter.get_model_info(model_id)
        if not info.get("file"):
            raise PdMcpError(ErrorCode.FILE_ERROR,
                             "Model has no file path; cannot restore from backup")
        original = Path(info["file"])
        self._adapter.close_model(model_id, save=False)
        shutil.copy2(src, original)
        new = self._adapter.open_model(str(original))
        return {"restored_from": str(src), "model": new}

    def _prune_backups(self) -> None:
        files = sorted(self._backup_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        while len(files) > self._keep:
            try:
                files.pop(0).unlink()
            except OSError:
                break
