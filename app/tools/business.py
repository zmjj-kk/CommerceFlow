"""Database-backed order facts. No generated ownership or shared demo orders."""
from app.db import repository


def snapshot(order) -> dict:
    return {"order_id": order.order_id, "status": order.status, "amount": str(order.amount),
            "product": order.product, "tracking_no": order.tracking_no,
            "created_at": order.created_at.isoformat() if order.created_at else None}


async def order_snapshot(order_id: str, user_id: str) -> dict | None:
    order = await repository.get_order_for_user(order_id, user_id)
    return snapshot(order) if order else None


async def owns_order(user_id: str, order_id: str) -> bool:
    return bool(user_id and order_id and await repository.get_order_for_user(order_id, user_id))


async def list_user_orders(user_id: str) -> list[dict]:
    return [snapshot(order) for order in await repository.list_orders_for_user(user_id)]
