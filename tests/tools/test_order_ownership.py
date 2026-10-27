import pytest
from tests.security.conftest import encoded_password, secure_db
from app.tools import business
from app.tools.builtin.orders import query_order
from app.tools.builtin.refunds import submit_refund


async def test_ownership_uses_real_owner(secure_db):
    assert await business.owns_order("alice", "1001")
    assert not await business.owns_order("bob", "1001")
    assert not await business.owns_order("", "1001")


async def test_read_own_order(secure_db):
    out = await query_order.ainvoke({"order_id": "1001", "user_id": "alice"})
    assert out["order_id"] == "1001" and out["tracking_no"] == "SF1001"


async def test_foreign_and_missing_are_indistinguishable(secure_db):
    foreign = await query_order.ainvoke({"order_id": "2002", "user_id": "alice"})
    missing = await query_order.ainvoke({"order_id": "9999", "user_id": "alice"})
    assert foreign == missing
    assert set(foreign) == {"error", "code"}


async def test_refund_proposal_checks_owner(secure_db):
    out = await submit_refund.ainvoke({"order_id": "2002", "user_id": "alice"})
    assert out["code"] == "order_not_owned"


async def test_no_shared_demo_orders(secure_db):
    assert not await business.owns_order("bob", "1001")
    assert not await business.owns_order("alice", "2002")
    assert await business.list_user_orders("unregistered") == []
