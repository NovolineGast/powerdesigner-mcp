"""FastMCP server assembly."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from ..server_context import Backend
from . import prompts, resources
from . import tools_advanced, tools_model, tools_modify, tools_read

log = logging.getLogger("pdmcp.server")

INSTRUCTIONS = """\
PowerDesigner Database Modeling Automation MCP.

 lets AI agents operate PowerDesigner (CDM/LDM/PDM) like a database modeling
 engineer: manage models, create tables/columns/primary keys/foreign
 keys/indexes/domains, apply structured schema patches, validate designs
 against rules, generate DDL and convert CDM->LDM->PDM - all through the
 official PowerDesigner COM Automation API (never by touching .pdm binaries).

Key behaviours:
* Every mutating tool accepts dry_run=true to preview the plan.
* apply_schema_patch / create_database_schema run atomically (auto-rollback).
* begin_transaction / rollback_transaction give exact file-level restore.
* Object references ("ref") returned by list/get tools can be passed to
  other tools; most tools also accept object codes for convenience.
* Recommended course-design loop: inspect -> design -> dry_run -> apply ->
  validate_model / check_database_design -> fix -> generate_ddl -> save_model.
"""


def create_server(backend: Backend | None = None) -> FastMCP:
    backend = backend or Backend()
    mcp = FastMCP("powerdesigner", instructions=INSTRUCTIONS)

    tools_model.register(mcp, backend)
    tools_read.register(mcp, backend)
    tools_modify.register(mcp, backend)
    tools_advanced.register(mcp, backend)
    resources.register(mcp, backend)
    prompts.register(mcp, backend)

    return mcp
