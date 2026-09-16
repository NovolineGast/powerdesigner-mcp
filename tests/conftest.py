"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from pd_mcp.powerdesigner.mock_adapter import MockAdapter  # noqa: E402
from pd_mcp.services.transactions import TransactionManager  # noqa: E402


@pytest.fixture()
def adapter(tmp_path):
    a = MockAdapter()
    a.connect()
    yield a
    a.disconnect()


@pytest.fixture()
def pdm(adapter):
    """A PDM model with two related tables, composite-ready."""
    mid = adapter.create_model("PDM", "Test Shop", "test_shop", "MySQL 5.0")["model_id"]
    users = adapter.create_table(mid, "User", "sys_user", "users")["ref"]
    adapter.create_column(mid, users, {"name": "UserId", "code": "user_id",
                                       "data_type": "INT", "mandatory": True,
                                       "primary": True})
    adapter.create_column(mid, users, {"name": "Phone", "code": "user_phone",
                                       "data_type": "VARCHAR(20)"})
    orders = adapter.create_table(mid, "Order", "sys_order", "orders")["ref"]
    adapter.create_column(mid, orders, {"name": "OrderId", "code": "order_id",
                                        "data_type": "INT", "mandatory": True,
                                        "primary": True})
    adapter.create_reference(mid, users, orders, name="FK_order_user")
    return {"adapter": adapter, "model_id": mid, "users": users, "orders": orders}


@pytest.fixture()
def cdm(adapter):
    """A CDM with two related entities and a primary identifier each.

    Deliberately built the way the real backend requires: attributes carry no
    primary flag (primacy comes from create_primary_key / the identifier) and
    the relationship is a plain association between entities.
    """
    mid = adapter.create_model("CDM", "Test Shop", "test_shop")["model_id"]
    cust = adapter.create_table(mid, "Customer", "customer", "buyers")["ref"]
    adapter.create_column(mid, cust, {"name": "CustomerId", "code": "customer_id",
                                      "data_type": "Integer", "mandatory": True})
    adapter.create_column(mid, cust, {"name": "Phone", "code": "phone",
                                      "data_type": "Characters(11)"})
    adapter.create_primary_key(mid, cust, ["customer_id"])
    addr = adapter.create_table(mid, "Address", "address")["ref"]
    adapter.create_column(mid, addr, {"name": "AddressId", "code": "address_id",
                                      "data_type": "Integer", "mandatory": True})
    adapter.create_primary_key(mid, addr, ["address_id"])
    adapter.create_reference(mid, cust, addr, name="rel_customer_address")
    return {"adapter": adapter, "model_id": mid, "customer": cust, "address": addr}


@pytest.fixture()
def txn_manager(adapter, tmp_path):
    return TransactionManager(adapter, tmp_path / "backups")
