"""Server-side context shared by all MCP tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import ServerConfig, get_config
from .errors import ComUnavailableError, PdMcpError
from .powerdesigner.adapter import PowerDesignerAdapter
from .services.transactions import TransactionManager


class Backend:
    """Holds the adapter (COM or mock) and the transaction manager."""

    def __init__(self, config: Optional[ServerConfig] = None):
        self.config = config or get_config()
        self._adapter: Optional[PowerDesignerAdapter] = None

    # ------------------------------------------------------------------
    def _create_adapter(self) -> PowerDesignerAdapter:
        choice = (os.environ.get("PDMCP_ADAPTER") or "auto").lower()
        if choice == "mock":
            from .powerdesigner.mock_adapter import MockAdapter
            return MockAdapter()
        if choice == "com":
            from .powerdesigner.com_adapter import ComAdapter
            return ComAdapter(self.config)
        # auto: use COM when pywin32 is importable, otherwise mock
        try:
            import win32com.client  # noqa: F401
            from .powerdesigner.com_adapter import ComAdapter
            return ComAdapter(self.config)
        except Exception:
            from .powerdesigner.mock_adapter import MockAdapter
            return MockAdapter()

    def adapter(self) -> PowerDesignerAdapter:
        if self._adapter is None:
            self._adapter = self._create_adapter()
            self._adapter.connect()
        return self._adapter

    def connected_adapter(self) -> PowerDesignerAdapter:
        """Adapter that must be connected; reconnects lazily when possible."""
        adapter = self.adapter()
        if not adapter.is_connected():
            try:
                adapter.connect()
            except PdMcpError as exc:
                if isinstance(exc, ComUnavailableError) and \
                        adapter.__class__.__name__ == "ComAdapter" and \
                        (os.environ.get("PDMCP_ALLOW_MOCK_FALLBACK", "0") == "1"):
                    self._adapter = self._create_adapter.__defaults__ and None  # pragma: no cover
                    from .powerdesigner.mock_adapter import MockAdapter
                    self._adapter = MockAdapter()
                    self._adapter.connect()
                    return self._adapter
                raise
        return adapter

    # ------------------------------------------------------------------
    def txn_manager(self) -> TransactionManager:
        return TransactionManager(self.adapter(), Path(self.config.backup_dir))

    def reset_adapter(self) -> None:
        self._adapter = None
