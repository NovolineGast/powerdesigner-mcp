"""Live integration tests - require a real PowerDesigner installation.

Run with:  pytest -m live   (auto-skipped unless PDMCP_LIVE=1)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("PDMCP_LIVE") != "1",
                       reason="live tests require PDMCP_LIVE=1 and PowerDesigner"),
]


@pytest.fixture(scope="module")
def live_adapter():
    from pd_mcp.config import ServerConfig
    from pd_mcp.powerdesigner.com_adapter import ComAdapter
    cfg = ServerConfig()
    cfg.attach_mode = os.environ.get("PDMCP_ATTACH_MODE", "auto")
    adapter = ComAdapter(cfg)
    adapter.connect()
    yield adapter
    adapter.disconnect()


def test_full_course_design_flow(live_adapter: object, tmp_path: Path):
    """Acceptance flow: create -> schema -> validate -> DDL -> save."""
    a = live_adapter
    mid = a.create_model("PDM", "Live_Shop", "LIVE_SHOP", "MySQL 5.0")["model_id"]

    spec = {
        "tables": [
            {"name": "User", "code": "sys_user", "comment": "users",
             "columns": [
                 {"code": "user_id", "data_type": "INT", "primary": True,
                  "mandatory": True, "comment": "PK"},
                 {"code": "username", "data_type": "VARCHAR(50)",
                  "mandatory": True, "comment": "login name"},
                 {"code": "user_phone", "data_type": "VARCHAR(20)",
                  "comment": "phone"}],
             "indexes": [{"name": "idx_user_phone", "columns": ["user_phone"]}]},
            {"name": "Order", "code": "sys_order", "comment": "orders",
             "columns": [
                 {"code": "order_id", "data_type": "INT", "primary": True,
                  "mandatory": True, "comment": "PK"},
                 {"code": "user_id", "data_type": "INT", "comment": "buyer"}]},
        ],
        "relationships": [
            {"parent_table": "sys_user", "child_table": "sys_order",
             "name": "FK_order_user", "cardinality": "0,n"},
        ],
    }
    from pd_mcp.services.schema_service import create_database_schema
    res = create_database_schema(a, mid, spec, atomic=True,
                                 txn_manager=_txn(a, tmp_path))
    assert res["success"], res

    from pd_mcp.services.validation import validate_model
    v = validate_model(a, mid)
    assert v["passed"], v["errors"]

    sql_path = tmp_path / "live.sql"
    ddl = a.generate_database(mid, str(sql_path))
    assert "create table" in ddl["sql"].lower()

    saved = a.save_model_as(mid, str(tmp_path / "live_model.pdm"))
    assert saved["saved"]

    counts = a.get_model_info(mid)["object_counts"]
    assert counts["tables"] == 2 and counts["references"] == 1


def _txn(adapter, tmp_path):
    from pd_mcp.services.transactions import TransactionManager
    return TransactionManager(adapter, tmp_path / "backups")
