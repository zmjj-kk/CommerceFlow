from app.services.tickets import submit


async def submit_refund_request(*, user_id: str, conversation_id: int, order_id: str,
                                reason: str, approval_id: str) -> str:
    """Creates a refund request ticket; no payment or money movement."""
    return await submit(user_id=user_id, conversation_id=conversation_id, kind="create_refund",
                        args={"order_id": order_id, "reason": reason}, approval_id=approval_id)
