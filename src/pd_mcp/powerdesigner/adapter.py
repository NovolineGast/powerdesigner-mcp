"""Adapter interface between the MCP layer and PowerDesigner.

The adapter is the ONLY place that knows how PowerDesigner is driven.
Two implementations exist:

* :class:`pd_mcp.powerdesigner.com_adapter.ComAdapter` - drives the real
  PowerDesigner through its COM Automation API (out-of-process, single
  dedicated COM thread).
* :class:`pd_mcp.powerdesigner.mock_adapter.MockAdapter` - an in-memory
  model store with identical behaviour, used for tests and dry development
  on machines without PowerDesigner.

All read methods return plain JSON-serialisable dicts; all mutating methods
raise :class:`pd_mcp.errors.PdMcpError` on failure.  COM objects never leak
past this boundary - services and tools only ever see dicts and string ids.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class PowerDesignerAdapter(ABC):
    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    @abstractmethod
    def connect(self) -> Dict[str, Any]:
        """Connect/attach to PowerDesigner. Returns server/app info."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the connection (quit PD only if we launched it and allowed)."""

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def server_info(self) -> Dict[str, Any]:
        """Version info, progid, attach mode, capability flags."""

    # ------------------------------------------------------------------
    # Model management
    # ------------------------------------------------------------------
    @abstractmethod
    def list_models(self) -> List[Dict[str, Any]]:
        """All models currently open in PowerDesigner."""

    @abstractmethod
    def open_model(self, path: str, read_only: bool = False) -> Dict[str, Any]:
        ...

    @abstractmethod
    def create_model(self, kind: str, name: str, code: str = "",
                     dbms: Optional[str] = None) -> Dict[str, Any]:
        """kind: PDM | CDM | LDM."""

    @abstractmethod
    def save_model(self, model_id: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def save_model_as(self, model_id: str, path: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def close_model(self, model_id: str, save: bool = False) -> Dict[str, Any]:
        ...

    @abstractmethod
    def get_model_info(self, model_id: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Packages
    # ------------------------------------------------------------------
    @abstractmethod
    def list_packages(self, model_id: str) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_package(self, model_id: str, package_ref: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Tables / Entities
    # ------------------------------------------------------------------
    @abstractmethod
    def list_tables(self, model_id: str, package_ref: Optional[str] = None,
                    query: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_table(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def create_table(self, model_id: str, name: str, code: str = "",
                     comment: str = "", columns: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """columns: optional list of column specs (same shape as create_column)."""

    @abstractmethod
    def update_table(self, model_id: str, table_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """updates: name/code/comment keys; None values ignored."""

    @abstractmethod
    def delete_table(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Columns / Attributes
    # ------------------------------------------------------------------
    @abstractmethod
    def list_columns(self, model_id: str, table_ref: str,
                     query: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_column(self, model_id: str, table_ref: str, column_ref: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def create_column(self, model_id: str, table_ref: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        """spec keys: name, code, data_type, length, precision, mandatory,
        default_value, comment, description, domain, primary."""

    @abstractmethod
    def update_column(self, model_id: str, table_ref: str, column_ref: str,
                      updates: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def delete_column(self, model_id: str, table_ref: str, column_ref: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Keys / Identifiers
    # ------------------------------------------------------------------
    @abstractmethod
    def list_keys(self, model_id: str, table_ref: str) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def create_primary_key(self, model_id: str, table_ref: str, columns: List[str],
                           name: str = "", code: str = "") -> Dict[str, Any]:
        """Replaces the current primary key with one built from `columns`."""

    @abstractmethod
    def remove_primary_key(self, model_id: str, table_ref: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # References (FK) / Relationships
    # ------------------------------------------------------------------
    @abstractmethod
    def list_references(self, model_id: str) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_reference(self, model_id: str, reference_ref: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def create_reference(self, model_id: str, parent_table: str, child_table: str,
                         parent_columns: Optional[List[str]] = None,
                         child_columns: Optional[List[str]] = None,
                         name: str = "", code: str = "", comment: str = "",
                         cardinality: Optional[str] = None,
                         update_key: bool = True) -> Dict[str, Any]:
        ...

    @abstractmethod
    def update_reference(self, model_id: str, reference_ref: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def delete_reference(self, model_id: str, reference_ref: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    @abstractmethod
    def list_indexes(self, model_id: str, table_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def create_index(self, model_id: str, table_ref: str, columns: List[str],
                     name: str = "", code: str = "", unique: bool = False,
                     comment: str = "") -> Dict[str, Any]:
        ...

    @abstractmethod
    def update_index(self, model_id: str, table_ref: str, index_ref: str,
                     updates: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def delete_index(self, model_id: str, table_ref: str, index_ref: str) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Domains
    # ------------------------------------------------------------------
    @abstractmethod
    def list_domains(self, model_id: str) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_domain(self, model_id: str, domain_ref: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def create_domain(self, model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------
    # Native model check / DDL / conversion
    # ------------------------------------------------------------------
    @abstractmethod
    def native_check_model(self, model_id: str) -> Dict[str, Any]:
        """Run PowerDesigner's own model check. Returns structured messages."""

    @abstractmethod
    def generate_database(self, model_id: str, output_path: str,
                          options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Generate DDL via the model's own DBMS. Returns file info + sql text."""

    @abstractmethod
    def convert_model(self, model_id: str, target_kind: str,
                      dbms: Optional[str] = None) -> Dict[str, Any]:
        """Native conversion: CDM->LDM, CDM->PDM, LDM->PDM."""

    # ------------------------------------------------------------------
    # Object handle utilities (for transactions / journal)
    # ------------------------------------------------------------------
    @abstractmethod
    def delete_object_by_ref(self, model_id: str, kind: str, obj_ref: str) -> Dict[str, Any]:
        """Delete any tracked object (table/column/reference/index/domain/key)."""

    @abstractmethod
    def update_object_props(self, model_id: str, kind: str, obj_ref: str,
                            props: Dict[str, Any]) -> Dict[str, Any]:
        """Set a subset of {name, code, comment} on a tracked object."""

    @abstractmethod
    def get_object_props(self, model_id: str, kind: str, obj_ref: str) -> Dict[str, Any]:
        """Read {name, code, comment} of a tracked object."""
