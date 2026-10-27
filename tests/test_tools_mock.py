from tests.security.conftest import encoded_password, secure_db
from app.tools.builtin.orders import query_order, query_product


async def test_query_order_database_snapshot(secure_db):
    r1 = await query_order.ainvoke({"order_id": "1001", "user_id": "alice"})
    r2 = await query_order.ainvoke({"order_id": "1001", "user_id": "alice"})
    assert r1 == r2 and r1["order_id"] == "1001" and r1["tracking_no"] == "SF1001"


async def test_query_product_names_and_determinism():
    p1 = await query_product.ainvoke({"product_name": "猫粮"})
    p2 = await query_product.ainvoke({"product_name": "猫粮"})
    assert p1 == p2 and p1["product_name"] == "猫粮"
