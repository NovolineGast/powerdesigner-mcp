"""High-level tools: schema creation, patches, design-from-spec, validation,
rule engine, DDL generation, model conversion, transactions."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..errors import tool_result
from ..server_context import Backend
from ..services.schema_service import (
    apply_schema_patch as svc_apply_patch,
    create_database_schema as svc_create_schema,
    design_from_spec as svc_design,
)
from ..services.validation import check_database_design as svc_rules
from ..services.validation import validate_model as svc_validate

TOOL_DEFS = []


def register(mcp, backend: Backend) -> None:
    # ------------------------------------------------------------------
    # High-level schema tools
    # ------------------------------------------------------------------
    @mcp.tool(name="create_database_schema", description=(
        "Create a whole schema in one call from a JSON spec: "
        "{tables:[{name, code, comment, columns:[{name, code, data_type, length, "
        "precision, mandatory, default_value, comment, primary}], indexes:[...]}], "
        "relationships:[{parent_table, child_table, parent_columns, child_columns, "
        "name, cardinality, parent_cardinality, dependent_role}], domains:[...], "
        "indexes:[...]}. Primary keys are derived from columns with primary:true "
        "(or primary_key_columns). The plan follows the target model's kind: in a "
        "CDM/LDM 'tables' are entities, columns are attributes rendered in "
        "PowerDesigner's own type vocabulary, primary keys become identifiers, "
        "relationships become associations (cardinality = child end, "
        "parent_cardinality = parent end), and index entries are rejected. "
        "dry_run=true returns the execution plan without touching the model; "
        "atomic=true (default) rolls the model back if any step fails."))
    @tool_result
    def create_database_schema(model_id: str, spec: dict, dry_run: bool = False,
                               atomic: bool = True) -> dict:
        adapter = backend.connected_adapter()
        return svc_create_schema(adapter, model_id, spec, dry_run, atomic,
                                 backend.txn_manager())

    @mcp.tool(name="apply_schema_patch", description=(
        "Apply an ordered list of structured patch operations to a model. "
        "operations: [{op: create_table|alter_table|rename_table|drop_table|"
        "add_column|alter_column|drop_column|create_primary_key|drop_primary_key|"
        "create_reference|alter_reference|drop_reference|create_index|alter_index|"
        "drop_index|create_domain, ...}]. Execution stops at the first failure and "
        "(atomic, default) rolls back. dry_run=true returns the plan."))
    @tool_result
    def apply_schema_patch(model_id: str, operations: list, dry_run: bool = False,
                           atomic: bool = True) -> dict:
        adapter = backend.connected_adapter()
        return svc_apply_patch(adapter, model_id, operations, dry_run, atomic,
                               backend.txn_manager())

    @mcp.tool(name="design_from_spec", description=(
        "Materialise a full design spec into a NEW model (or extend an existing "
        "one via model_id). spec: {model:{kind: 'PDM'|'CDM'|'LDM', name, code, "
        "dbms}, tables/entities:[...], relationships:[...], domains:[...], "
        "indexes:[...]}. The AI supplies the design; this tool executes it. "
        "Supports dry_run."))
    @tool_result
    def design_from_spec(spec: dict, model_id: str = "", dry_run: bool = False,
                         atomic: bool = True) -> dict:
        adapter = backend.connected_adapter()
        return svc_design(adapter, spec, model_id or None, dry_run, atomic,
                          backend.txn_manager())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @mcp.tool(name="validate_model", description=(
        "Run built-in structural checks on a model: missing primary keys, "
        "duplicate table/column codes, missing data types, missing comments, "
        "FK type mismatches, dangling references, duplicate/redundant indexes. "
        "Returns passed/errors/warnings with object locations - fix issues and "
        "re-run to close the loop."))
    @tool_result
    def validate_model(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return svc_validate(adapter, model_id)

    @mcp.tool(name="check_database_design", description=(
        "Check a model against YOUR design rules. rules: [{id, description, "
        "type, params?, severity?}]. Machine-checkable types: TABLE_HAS_PK, "
        "TABLE_CODE_PATTERN, TABLE_NAME_STYLE, COLUMN_HAS_COMMENT, "
        "COLUMN_CODE_PATTERN, COLUMN_MANDATORY, REQUIRED_COLUMNS, "
        "PK_NAME_PATTERN, FK_NAME_PATTERN, INDEX_NAME_PATTERN, "
        "DATA_TYPE_ALLOWED, NO_DUPLICATE_INDEX, NO_EMPTY_COMMENT_TABLE, REGEX. "
        "Rules with unknown types are reported in 'skipped' (never silently "
        "ignored). Returns passed/errors/warnings/violations."))
    @tool_result
    def check_database_design(model_id: str, rules: list) -> dict:
        adapter = backend.connected_adapter()
        return svc_rules(adapter, model_id, rules)

    # ------------------------------------------------------------------
    # DDL + conversion
    # ------------------------------------------------------------------
    @mcp.tool(name="generate_ddl", description=(
        "Generate SQL DDL for a PDM using PowerDesigner's native database "
        "generation for the model's DBMS. output_path: target .sql file "
        "(created if the directory exists). Returns the SQL text plus file info. "
        "The model's own DBMS is used; requesting a different dbms returns a "
        "clear error."))
    @tool_result
    def generate_ddl(model_id: str, output_path: str, dbms: str = "") -> dict:
        adapter = backend.connected_adapter()
        options = {"dbms": dbms} if dbms else None
        return {"success": True, **adapter.generate_database(model_id, output_path, options)}

    @mcp.tool(name="convert_cdm_to_ldm", description=(
        "Convert a CDM model to an LDM. Uses PowerDesigner's native conversion "
        "when available, otherwise a structured mapping. Returns the new model "
        "info."))
    @tool_result
    def convert_cdm_to_ldm(model_id: str) -> dict:
        adapter = backend.connected_adapter()
        return {"success": True, **adapter.convert_model(model_id, "LDM")}

    @mcp.tool(name="convert_cdm_to_pdm", description=(
        "Convert a CDM model to a PDM (optionally pass dbms, e.g. 'MySQL 5.0'). "
        "Native conversion first, structured mapping as fallback."))
    @tool_result
    def convert_cdm_to_pdm(model_id: str, dbms: str = "") -> dict:
        adapter = backend.connected_adapter()
        return {"success": True,
                **adapter.convert_model(model_id, "PDM", dbms or None)}

    @mcp.tool(name="convert_ldm_to_pdm", description=(
        "Convert an LDM model to a PDM (optionally pass dbms). Native "
        "conversion first, structured mapping as fallback."))
    @tool_result
    def convert_ldm_to_pdm(model_id: str, dbms: str = "") -> dict:
        adapter = backend.connected_adapter()
        return {"success": True,
                **adapter.convert_model(model_id, "PDM", dbms or None)}

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------
    @mcp.tool(name="begin_transaction", description=(
        "Start a transaction before a batch of modifications. For file-backed "
        "models the current file is saved and backed up so rollback can restore "
        "the exact pre-transaction state. Pass the returned txn_id to "
        "commit_transaction / rollback_transaction."))
    @tool_result
    def begin_transaction(model_id: str, auto_save: bool = True) -> dict:
        return {"success": True, **backend.txn_manager().begin(model_id, auto_save)}

    @mcp.tool(name="commit_transaction", description=(
        "Commit a transaction: keeps all changes and discards the backup."))
    @tool_result
    def commit_transaction(txn_id: str) -> dict:
        return {"success": True, **backend.txn_manager().commit(txn_id)}

    @mcp.tool(name="rollback_transaction", description=(
        "Roll a transaction back: file-backed models are restored from the "
        "pre-transaction backup and reopened (the model_id may change - run "
        "list_open_models). Without a file backup, create/update operations are "
        "undone via the operation journal; deletions without backup cannot be "
        "undone and are reported explicitly."))
    @tool_result
    def rollback_transaction(txn_id: str) -> dict:
        return {"success": True, **backend.txn_manager().rollback(txn_id)}

    @mcp.tool(name="list_model_backups", description=(
        "List available model backup files (created by transactions and "
        "save operations) in the backup directory."))
    @tool_result
    def list_model_backups() -> dict:
        return {"success": True, "backups": backend.txn_manager().list_backups()}

    @mcp.tool(name="rollback_model", description=(
        "Restore a model from a backup file (latest backup, or pass "
        "backup_file from list_model_backups). Closes the model, restores the "
        "file and reopens it."))
    @tool_result
    def rollback_model(model_id: str, backup_file: str = "") -> dict:
        return {"success": True, **backend.txn_manager().rollback_model(
            model_id, backup_file or None)}
