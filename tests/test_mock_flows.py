"""End-to-end adapter behaviour tests on the mock backend."""

from __future__ import annotations

import pytest

from pd_mcp.errors import InvalidParamsError, ObjectNotFoundError, PdMcpError


class TestModelManagement:
    def test_create_and_list(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        models = adapter.list_models()
        assert any(m["model_id"] == mid for m in models)
        info = adapter.get_model_info(mid)
        assert info["kind"] == "PDM" and info["dbms"]

    def test_invalid_kind(self, adapter):
        with pytest.raises(InvalidParamsError):
            adapter.create_model("XXX", "M", "m")

    def test_open_missing_file(self, adapter):
        with pytest.raises(ObjectNotFoundError):
            adapter.open_model(r"E:\no_such.pdm")

    def test_save_unsaved_raises(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        with pytest.raises(PdMcpError):
            adapter.save_model(mid)

    def test_save_as_writes_file(self, adapter, tmp_path):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        res = adapter.save_model_as(mid, str(tmp_path / "m.pdm"))
        assert res["saved"]
        assert (tmp_path / "m.pdm").exists()

    def test_close(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        adapter.close_model(mid)
        assert adapter.get_model_info.__self__ is adapter
        from pd_mcp.errors import ModelNotFoundError
        with pytest.raises(ModelNotFoundError):
            adapter.get_model_info(mid)


class TestTablesAndColumns:
    def test_table_crud(self, pdm):
        a, mid = pdm["adapter"], pdm["model_id"]
        t = a.create_table(mid, "Product", "sys_product", "products")
        assert t["code"] == "sys_product"
        a.update_table(mid, "sys_product", {"comment": "updated"})
        assert a.get_table(mid, "sys_product")["comment"] == "updated"
        a.delete_table(mid, "sys_product")
        with pytest.raises(ObjectNotFoundError):
            a.get_table(mid, "sys_product")

    def test_duplicate_table_code(self, pdm):
        with pytest.raises(PdMcpError):
            pdm["adapter"].create_table(pdm["model_id"], "User2", "sys_user")

    def test_column_crud_and_types(self, pdm):
        a, mid, users = pdm["adapter"], pdm["model_id"], pdm["users"]
        col = a.create_column(mid, users, {
            "name": "Email", "code": "email", "data_type": "VARCHAR(100)",
            "mandatory": True, "default_value": "'n/a'", "comment": "email"})
        assert col["data_type"] == "VARCHAR(100)" and col["mandatory"]
        a.update_column(mid, users, "email", {"data_type": "VARCHAR(200)"})
        assert a.get_column(mid, users, "email")["data_type"] == "VARCHAR(200)"
        a.delete_column(mid, users, "email")
        assert all(c["code"] != "email" for c in a.list_columns(mid, users))

    def test_duplicate_column_code(self, pdm):
        with pytest.raises(PdMcpError):
            pdm["adapter"].create_column(pdm["model_id"], pdm["users"],
                                         {"name": "U", "code": "user_id"})


class TestPrimaryKeys:
    def test_single_pk(self, pdm):
        a, mid, orders = pdm["adapter"], pdm["model_id"], pdm["orders"]
        pk = a.create_primary_key(mid, orders, ["order_id"], name="PK_sys_order")
        assert pk["primary"] and pk["columns"][0]["code"] == "order_id"

    def test_composite_pk(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        t = adapter.create_table(mid, "OrderItem", "order_item")["ref"]
        for code in ("order_id", "product_id"):
            adapter.create_column(mid, t, {"name": code, "code": code,
                                           "data_type": "INT"})
        pk = adapter.create_primary_key(mid, t, ["order_id", "product_id"])
        codes = [c["code"] for c in pk["columns"]]
        assert codes == ["order_id", "product_id"]

    def test_replace_pk(self, pdm):
        a, mid, users = pdm["adapter"], pdm["model_id"], pdm["users"]
        a.create_column(mid, users, {"name": "Login", "code": "login",
                                     "data_type": "VARCHAR(50)"})
        pk = a.create_primary_key(mid, users, ["login"], name="PK_login")
        assert [c["code"] for c in pk["columns"]] == ["login"]
        cols = {c["code"]: c for c in a.list_columns(mid, users)}
        assert not cols["user_id"]["primary"] and cols["login"]["primary"]

    def test_remove_pk(self, pdm):
        a, mid, users = pdm["adapter"], pdm["model_id"], pdm["users"]
        a.remove_primary_key(mid, users)
        assert all(not c["primary"] for c in a.list_columns(mid, users))


class TestReferences:
    def test_auto_fk_migration(self, pdm):
        a, mid = pdm["adapter"], pdm["model_id"]
        orders = a.get_table(mid, "sys_order")
        fk_cols = [c["code"] for c in orders["columns"]]
        assert "user_id" in fk_cols, "update_key should migrate parent PK into child"
        refs = a.list_references(mid)
        assert refs and refs[0]["parent_table"] == "sys_user"
        assert refs[0]["joins"] or refs[0]["parent_columns"]

    def test_explicit_columns(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        p = adapter.create_table(mid, "P", "p")["ref"]
        c = adapter.create_table(mid, "C", "c")["ref"]
        for tbl, code in ((p, "a"), (c, "a")):
            adapter.create_column(mid, tbl, {"name": code, "code": code,
                                             "data_type": "INT"})
        ref = adapter.create_reference(mid, p, c, ["a"], ["a"], name="FK_c")
        assert ref["parent_columns"] and ref["child_columns"]

    def test_delete_reference(self, pdm):
        a, mid = pdm["adapter"], pdm["model_id"]
        ref = a.list_references(mid)[0]
        a.delete_reference(mid, ref["ref"])
        assert a.list_references(mid) == []


class TestIndexes:
    def test_unique_and_composite(self, pdm):
        a, mid, orders = pdm["adapter"], pdm["model_id"], pdm["orders"]
        a.create_column(mid, orders, {"name": "CreatedAt", "code": "create_time",
                                      "data_type": "DATETIME"})
        idx = a.create_index(mid, orders, ["user_id", "create_time"],
                             name="idx_order_user_time")
        assert idx["columns"][0]["code"] == "user_id"
        u = a.create_index(mid, orders, ["order_id"], name="uq_order", unique=True)
        assert u["unique"]
        a.delete_index(mid, orders, "idx_order_user_time")
        names = [i["code"] for i in a.list_indexes(mid, orders)]
        assert "idx_order_user_time" not in names

    def test_index_needs_columns(self, pdm):
        with pytest.raises(InvalidParamsError):
            pdm["adapter"].create_index(pdm["model_id"], pdm["orders"], [])


class TestDomains:
    def test_domain_crud(self, adapter):
        mid = adapter.create_model("PDM", "M", "m")["model_id"]
        dom = adapter.create_domain(mid, {"name": "Money", "code": "MONEY",
                                          "data_type": "DECIMAL", "length": 12,
                                          "precision": 2})
        assert dom["data_type"] == "DECIMAL"
        assert any(d["code"] == "MONEY" for d in adapter.list_domains(mid))


class TestDdlAndConversion:
    def test_generate_database_mock(self, pdm, tmp_path):
        out = tmp_path / "out.sql"
        res = pdm["adapter"].generate_database(pdm["model_id"], str(out))
        sql = out.read_text(encoding="utf-8")
        assert "CREATE TABLE `sys_user`" in sql
        assert "PRIMARY KEY" in sql
        assert res["bytes"] > 0

    def test_convert_cdm_to_pdm(self, adapter):
        cdm = adapter.create_model("CDM", "Shop", "shop")["model_id"]
        e1 = adapter.create_table(cdm, "Customer", "customer")["ref"]
        adapter.create_column(cdm, e1, {"name": "CustId", "code": "cust_id",
                                        "data_type": "INT", "primary": True})
        e2 = adapter.create_table(cdm, "Address", "address")["ref"]
        adapter.create_column(cdm, e2, {"name": "AddrId", "code": "addr_id",
                                        "data_type": "INT", "primary": True})
        adapter.create_reference(cdm, e1, e2, name="rel_cust_addr")
        res = adapter.convert_model(cdm, "PDM")
        tid = res["target_model"]["model_id"]
        assert res["target_kind"] == "PDM"
        tables = {t["code"] for t in adapter.list_tables(tid)}
        assert {"customer", "address"} <= tables
        refs = adapter.list_references(tid)
        assert len(refs) == 1
