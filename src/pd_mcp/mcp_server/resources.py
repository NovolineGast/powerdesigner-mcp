"""MCP Resources - read-only model context via powerdesigner:// URIs."""

from __future__ import annotations

import json


def register(mcp, backend) -> None:
    @mcp.resource("powerdesigner://models",
                  name="Open models",
                  description="All models currently open in PowerDesigner.")
    def models() -> str:
        adapter = backend.connected_adapter()
        return json.dumps({"models": adapter.list_models()}, ensure_ascii=False, indent=1)

    @mcp.resource("powerdesigner://model/{model_id}",
                  name="Model info",
                  description="Detailed info of one open model.")
    def model_info(model_id: str) -> str:
        adapter = backend.connected_adapter()
        return json.dumps(adapter.get_model_info(model_id), ensure_ascii=False, indent=1)

    @mcp.resource("powerdesigner://model/{model_id}/tables",
                  name="Model tables",
                  description="Brief table list of one open model.")
    def model_tables(model_id: str) -> str:
        adapter = backend.connected_adapter()
        return json.dumps({"tables": adapter.list_tables(model_id)},
                          ensure_ascii=False, indent=1)

    @mcp.resource("powerdesigner://model/{model_id}/table/{table_ref}",
                  name="Table detail",
                  description="Full detail (columns, keys, indexes, references) of a table.")
    def table_detail(model_id: str, table_ref: str) -> str:
        adapter = backend.connected_adapter()
        return json.dumps(adapter.get_table(model_id, table_ref),
                          ensure_ascii=False, indent=1)

    @mcp.resource("powerdesigner://model/{model_id}/relationships",
                  name="Model relationships",
                  description="References (PDM) or relationships (CDM/LDM) of a model.")
    def model_relationships(model_id: str) -> str:
        adapter = backend.connected_adapter()
        return json.dumps({"relationships": adapter.list_references(model_id)},
                          ensure_ascii=False, indent=1)

    @mcp.resource("powerdesigner://model/{model_id}/indexes",
                  name="Model indexes",
                  description="All indexes of a model.")
    def model_indexes(model_id: str) -> str:
        adapter = backend.connected_adapter()
        return json.dumps({"indexes": adapter.list_indexes(model_id)},
                          ensure_ascii=False, indent=1)
