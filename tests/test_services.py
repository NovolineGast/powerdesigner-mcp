"""Tests for schema ops, validation, inspection and transactions."""

from __future__ import annotations

import pytest

from pd_mcp.errors import InvalidParamsError, PdMcpError
from pd_mcp.services.inspect import compare_model, inspect_schema, model_snapshot
from pd_mcp.services.schema_service import (
    apply_schema_patch,
    build_schema_ops,
    create_database_schema,
    design_from_spec,
)
from pd_mcp.services.validation import check_database_design, validate_model


class TestSchemaOps:
    def test_build_ops_counts(self):
        spec = {
            "tables": [
                {"name": "User", "code": "sys_user",
                 "columns": [{"code": "user_id", "data_type": "INT", "primary": True},
                             {"code": "username", "data_type": "VARCHAR(50)"}]},
            ],
            "relationships": [
                {"parent_table": "sys_user", "child_table": "sys_order",
                 "name": "FK_order_user"},
            ],
        }
        ops = build_schema_ops(spec)
        summary = {o["op"] for o in ops}
        assert "create_table" in summary and "add_column" in summary
        assert "create_primary_key" in summary and "create_reference" in summary

    def test_create_database_schema_end_to_end(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        spec = {
            "domains": [{"name": "Money", "code": "MONEY", "data_type": "DECIMAL(12,2)"}],
            "tables": [
                {"name": "Category", "code": "sys_category",
                 "columns": [{"code": "category_id", "data_type": "INT", "primary": True},
                             {"code": "category_name", "data_type": "VARCHAR(50)"}]},
                {"name": "Product", "code": "sys_product",
                 "columns": [{"code": "product_id", "data_type": "INT", "primary": True},
                             {"code": "category_id", "data_type": "INT"},
                             {"code": "price", "data_type": "DECIMAL(12,2)"}],
                 "indexes": [{"name": "idx_product_cat",
                              "columns": ["category_id"]}]},
            ],
            "relationships": [
                {"parent_table": "sys_category", "child_table": "sys_product",
                 "name": "FK_product_category"},
            ],
        }
        res = create_database_schema(adapter, mid, spec)
        assert res["success"]
        tables = {t["code"] for t in adapter.list_tables(mid)}
        assert {"sys_category", "sys_product"} <= tables
        refs = adapter.list_references(mid)
        assert any(r["code"] == "FK_product_category" for r in refs)
        # FK column was auto-migrated
        cols = {c["code"] for c in adapter.get_table(mid, "sys_product")["columns"]}
        assert "category_id" in cols

    def test_dry_run_touches_nothing(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        spec = {"tables": [{"code": "t1",
                            "columns": [{"code": "id", "data_type": "INT", "primary": True}]}]}
        res = create_database_schema(adapter, mid, spec, dry_run=True)
        assert res["dry_run"] and len(res["plan"]) >= 2
        assert adapter.list_tables(mid) == []

    def test_atomic_rollback_on_failure(self, adapter, txn_manager):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        adapter.save_model_as(mid, str(txn_manager._backup_dir.parent / "m.pdm"))
        ops = [
            {"op": "create_table", "table": {"name": "A", "code": "tbl_a"}},
            {"op": "create_table", "table": {"name": "B", "code": "tbl_b"}},
            {"op": "add_column", "table": "missing_table",
             "column": {"code": "x", "data_type": "INT"}},
        ]
        res = apply_schema_patch(adapter, mid, ops, txn_manager=txn_manager, atomic=True)
        assert not res["success"] and res.get("rolled_back")
        # rollback restored the pre-transaction file state: no tables
        new_mid = res["error"]["details"].get("new_model_id") or mid
        # after file restore the model id may change; locate it
        models = adapter.list_models()
        assert models, "model should be reopened after rollback"
        opened = models[0]["model_id"]
        assert adapter.list_tables(opened) == []

    def test_patch_known_operations(self, pdm):
        a, mid, orders = pdm["adapter"], pdm["model_id"], pdm["orders"]
        ops = [
            {"op": "alter_table", "table": "sys_order",
             "updates": {"comment": "customer orders"}},
            {"op": "add_column", "table": "sys_order",
             "column": {"code": "amount", "data_type": "DECIMAL(10,2)"}},
            {"op": "alter_column", "table": "sys_order", "column": "amount",
             "updates": {"mandatory": True}},
            {"op": "create_index", "table": "sys_order",
             "index": {"name": "idx_amount", "columns": ["amount"]}},
            {"op": "drop_index", "table": "sys_order", "index": "idx_amount"},
            {"op": "drop_column", "table": "sys_order", "column": "amount"},
        ]
        res = apply_schema_patch(a, mid, ops)
        assert res["success"] and len(res["executed"]) == 6

    def test_design_from_spec_creates_model(self, adapter):
        spec = {
            "model": {"kind": "PDM", "name": "Shop", "code": "shop", "dbms": "MySQL 5.0"},
            "tables": [{"name": "User", "code": "sys_user",
                        "columns": [{"code": "user_id", "data_type": "INT",
                                     "primary": True}]}],
        }
        res = design_from_spec(adapter, spec)
        assert res["success"] and res["created_model"]["kind"] == "PDM"
        assert len(adapter.list_tables(res["model_id"])) == 1

    def test_design_from_spec_cdm(self, adapter):
        """The whole CDM pipeline must run without a PDM-style index step."""
        spec = {
            "model": {"kind": "CDM", "name": "Shop", "code": "shop"},
            "tables": [
                {"name": "Customer", "code": "customer",
                 "columns": [{"code": "customer_id", "data_type": "Integer",
                              "primary": True},
                             {"code": "phone", "data_type": "varchar",
                              "length": 11}]},
                {"name": "Address", "code": "address",
                 "columns": [{"code": "address_id", "data_type": "Integer",
                              "primary": True}]},
            ],
            "relationships": [
                {"parent_table": "customer", "child_table": "address",
                 "name": "rel_customer_address", "cardinality": "0,n",
                 "parent_cardinality": "1,1", "dependent_role": "2"},
            ],
        }
        res = design_from_spec(adapter, spec)
        assert res["success"], res
        mid = res["model_id"]
        assert adapter.get_model_info(mid)["kind"] == "CDM"

        cust = adapter.get_table(mid, "customer")
        assert [c["code"] for c in cust["columns"]] == ["customer_id", "phone"]
        # primacy is an identifier here, not a per-attribute flag
        assert cust["primary_key"] is not None
        assert cust["primary_key"]["code"] == "ID_customer"
        assert all(c["primary"] is False for c in cust["columns"])

        rel = adapter.list_references(mid)[0]
        assert rel["code"] == "rel_customer_address"
        assert rel["cardinality"] == "0,n" and rel["dependent_role"] == "2"
        assert adapter.list_indexes(mid) == []

    def test_design_from_spec_cdm_plan_uses_entity_vocabulary(self, adapter):
        spec = {"model": {"kind": "CDM", "name": "S", "code": "s"},
                "tables": [{"code": "customer",
                            "columns": [{"code": "customer_id",
                                         "data_type": "Integer", "primary": True}]}]}
        res = design_from_spec(adapter, spec, dry_run=True)
        assert res["dry_run"]
        assert res["plan"][0] == "CREATE ENTITY customer"
        assert res["plan"][1] == "ADD ATTRIBUTE customer.customer_id"
        assert res["plan"][2] == "IDENTIFIER customer(customer_id)"

    def test_design_from_spec_cdm_rejects_indexes(self, adapter):
        spec = {"model": {"kind": "CDM", "name": "S", "code": "s"},
                "tables": [{"code": "customer",
                            "indexes": [{"columns": ["customer_id"]}]}]}
        with pytest.raises(InvalidParamsError):
            design_from_spec(adapter, spec, dry_run=True)

    def test_create_database_schema_follows_model_kind(self, adapter):
        """create_database_schema must not assume PDM on a CDM model."""
        cdm = adapter.create_model("CDM", "Shop", "shop")["model_id"]
        res = create_database_schema(
            adapter, cdm,
            {"tables": [{"code": "customer",
                         "columns": [{"code": "customer_id",
                                      "data_type": "Integer", "primary": True}]}]})
        assert res["success"], res
        assert res["executed"][0].startswith("CREATE ENTITY")
        assert adapter.get_table(cdm, "customer")["primary_key"] is not None


class TestValidation:
    def test_validate_clean_model(self, pdm):
        res = validate_model(pdm["adapter"], pdm["model_id"])
        # fixture model has comments missing -> warnings only, no errors
        assert res["passed"], res["errors"]

    def test_validate_missing_pk(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        adapter.create_table(mid, "T", "t1")
        res = validate_model(adapter, mid)
        assert not res["passed"]
        assert any(e["code"] == "TABLE_NO_PK" for e in res["errors"])

    def test_validate_fk_type_mismatch(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        p = adapter.create_table(mid, "P", "p")["ref"]
        adapter.create_column(mid, p, {"code": "id", "data_type": "INT", "primary": True})
        c = adapter.create_table(mid, "C", "c")["ref"]
        adapter.create_column(mid, c, {"code": "id", "data_type": "INT", "primary": True})
        adapter.create_column(mid, c, {"code": "pid", "data_type": "VARCHAR(10)"})
        adapter.create_reference(mid, p, c, ["id"], ["pid"], name="FK_c")
        res = validate_model(adapter, mid)
        assert any(e["code"] == "REF_TYPE_MISMATCH" for e in res["errors"])

    def test_rule_engine(self, pdm):
        rules = [
            {"id": "R1", "description": "all tables need PK",
             "type": "TABLE_HAS_PK"},
            {"id": "R2", "description": "snake case",
             "type": "TABLE_NAME_STYLE", "params": {"style": "snake_case"}},
            {"id": "R3", "description": "tables must have created_at",
             "type": "REQUIRED_COLUMNS", "params": {"columns": ["created_at"]}},
            {"id": "R4", "description": "unknown rule from AI",
             "type": "SOME_BUSINESS_RULE"},
        ]
        res = check_database_design(pdm["adapter"], pdm["model_id"], rules)
        assert not res["passed"]  # missing created_at
        assert any(v["rule_id"] == "R3" for v in res["violations"])
        assert any(s["rule_id"] == "R4" for s in res["skipped"])
        assert "TABLE_HAS_PK" in res["rules_checked"]

    def test_rule_engine_regex(self, pdm):
        rules = [{"id": "N1", "description": "pk naming", "type": "REGEX",
                  "params": {"target": "table_code", "pattern": r"sys_.*"}}]
        res = check_database_design(pdm["adapter"], pdm["model_id"], rules)
        assert res["passed"]


class TestInspection:
    def test_snapshot_summary_and_detail(self, pdm):
        snap = model_snapshot(pdm["adapter"], pdm["model_id"], "summary")
        assert snap["tables"] and snap["references"]
        detail = model_snapshot(pdm["adapter"], pdm["model_id"], "detail")
        user = next(t for t in detail["tables"] if t["code"] == "sys_user")
        assert user["columns"] and user["primary_key"]

    def test_inspect_schema_pagination(self, pdm):
        res = inspect_schema(pdm["adapter"], pdm["model_id"], "summary",
                             page=1, page_size=1)
        assert res["tables"]["total"] == 2 and res["tables"]["has_more"]
        assert res["foreign_keys"]

    def test_compare_model(self, adapter):
        m1 = adapter.create_model("PDM", "A", "a")["model_id"]
        m2 = adapter.create_model("PDM", "B", "b")["model_id"]
        adapter.create_table(m1, "T1", "t1")
        adapter.create_table(m2, "T1", "t1")
        adapter.create_table(m2, "T2", "t2")
        diff = compare_model(adapter, m1, m2)
        assert diff["tables_added"] == ["t2"]
        assert diff["tables_removed"] == []


class TestTransactions:
    def test_journal_rollback_create(self, adapter, txn_manager):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        txn = txn_manager.begin(mid)
        t = adapter.create_table(mid, "Temp", "temp_tbl")
        txn_manager.record(txn["txn_id"], {
            "type": "create_object", "model_id": mid, "kind": "table",
            "obj_ref": t["ref"], "code": "temp_tbl"})
        txn_manager.rollback(txn["txn_id"])
        assert adapter.list_tables(mid) == []

    def test_commit_keeps_changes(self, adapter, txn_manager):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        txn = txn_manager.begin(mid)
        adapter.create_table(mid, "Keep", "keep_tbl")
        txn_manager.commit(txn["txn_id"])
        assert any(t["code"] == "keep_tbl" for t in adapter.list_tables(mid))

    def test_unknown_txn(self, txn_manager):
        with pytest.raises(PdMcpError):
            txn_manager.commit("nope")
