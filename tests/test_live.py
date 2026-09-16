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
    # the requested file must hold the model - PD 16.5 otherwise writes an
    # extension-less sibling and leaves a ~2 KB ShellNew stub behind
    assert (tmp_path / "live_model.pdm").stat().st_size > 5_000
    assert not (tmp_path / "live_model").exists()

    counts = a.get_model_info(mid)["object_counts"]
    assert counts["tables"] == 2 and counts["references"] == 1


def _txn(adapter, tmp_path):
    from pd_mcp.services.transactions import TransactionManager
    return TransactionManager(adapter, tmp_path / "backups")


def test_conceptual_flow_cdm_to_ldm(live_adapter: object, tmp_path: Path):
    """CDM/LDM are first-class: attributes, identifiers, relationships.

    Every assertion here encodes a live-verified behaviour of PD 16.5, so this
    test doubles as the regression net for the COM quirks documented in
    docs/com-api-notes.md.
    """
    from pd_mcp.errors import InvalidParamsError

    a = live_adapter
    mid = a.create_model("CDM", "Live_Conceptual", "LIVE_CDM")["model_id"]
    assert a.get_model_info(mid)["kind"] == "CDM"

    cust = a.create_table(mid, "Customer", "customer", "buyers")["ref"]
    a.create_column(mid, cust, {"name": "CustomerId", "code": "customer_id",
                                "data_type": "INT", "mandatory": True,
                                "comment": "primary identifier"})
    a.create_column(mid, cust, {"name": "Phone", "code": "phone",
                                "data_type": "varchar", "length": 11})
    addr = a.create_table(mid, "Address", "address", "delivery addresses")["ref"]
    a.create_column(mid, addr, {"name": "AddressId", "code": "address_id",
                                "data_type": "INT", "mandatory": True})
    a.create_column(mid, addr, {"name": "Detail", "code": "detail",
                                "data_type": "nvarchar", "length": 100})

    # CDM attributes carry PowerDesigner's type vocabulary, never DBMS syntax,
    # and have no primary flag
    cols = {c["code"]: c for c in a.list_columns(mid, cust)}
    assert cols["phone"]["data_type"] == "Variable characters(11)", cols["phone"]
    assert cols["customer_id"]["data_type"] == "Integer", cols["customer_id"]
    assert cols["customer_id"]["primary"] is False

    pk = a.create_primary_key(mid, cust, ["customer_id"])
    assert pk["primary"], pk
    assert [c["code"] for c in pk["columns"]] == ["customer_id"]
    a.create_primary_key(mid, addr, ["address_id"])

    # replacing the identifier must leave exactly one behind, still primary
    a.create_column(mid, cust, {"name": "Alt", "code": "alt_key",
                                "data_type": "Integer"})
    a.create_primary_key(mid, cust, ["customer_id", "alt_key"])
    keys = a.list_keys(mid, cust)
    assert len(keys) == 1, keys
    assert keys[0]["primary"], keys

    # a conceptual relationship: both ends' multiplicities plus the dependent side
    rel = a.create_reference(mid, "customer", "address",
                             name="rel_customer_address", cardinality="0,n",
                             parent_cardinality="1,1", dependent_role="2")
    assert rel["parent_table"] == "customer" and rel["child_table"] == "address"
    assert rel["cardinality"] == "0,n" and rel["parent_cardinality"] == "1,1"
    assert rel["dependent_role"] == "B", rel

    listed = a.list_references(mid)
    assert len(listed) == 1 and listed[0]["code"] == "rel_customer_address"
    assert a.get_reference(mid, listed[0]["ref"])["parent_table"] == "customer"

    # indexes are a physical concept and must be refused, not silently ignored
    with pytest.raises(InvalidParamsError):
        a.create_index(mid, cust, ["phone"])

    native = a.native_check_model(mid)
    assert not native["errors"], native

    # save_model_as: the requested file must hold the model, not the ShellNew
    # stub, and the diagram symbols must come along.  PD 16.5 sometimes writes
    # the real content to an extension-less sibling instead of the requested
    # path (see docs/com-api-notes.md), which the adapter has to promote back.
    cdm_path = tmp_path / "live_cdm.cdm"
    saved = a.save_model_as(mid, str(cdm_path))
    assert saved["saved"], saved
    assert cdm_path.stat().st_size > 20_000, cdm_path.stat().st_size
    assert not (tmp_path / "live_cdm").exists(), \
        "PD's extension-less file was left behind"
    counts = a.get_model_info(mid)["object_counts"]
    assert counts["tables"] == 2 and counts["references"] == 1
    assert counts["symbols"] >= 3, counts

    # native generation applies PD's own mapping rules: the (composite)
    # identifier migrates into the entity at the (x,n) end
    conv = a.convert_model(mid, "LDM")
    assert conv["engine"] == "powerdesigner-native", conv
    assert conv["target_kind"] == "LDM", conv
    ldm_id = conv["target_model"]["model_id"]
    ldm_addr_cols = {c["code"] for c in a.list_columns(ldm_id, "address")}
    assert {"customer_id", "alt_key"} <= ldm_addr_cols, ldm_addr_cols

    ldm_path = tmp_path / "live_ldm.ldm"
    assert a.save_model_as(ldm_id, str(ldm_path))["saved"]
    assert ldm_path.stat().st_size > 20_000, ldm_path.stat().st_size
    assert not (tmp_path / "live_ldm").exists()
    a.close_model(ldm_id)
