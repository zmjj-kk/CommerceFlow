"""Real MySQL DDL, token rotation, approvals and concurrent business transactions."""
import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.config import settings
from app.db import repository
from app.db.models import BusinessOperation, Order, RefundRequest, Ticket
from app.security.errors import SecurityError
from app.security.tokens import now_utc, verify_access
from app.services import auth_service, tickets
from app.services.refunds import submit_refund_request


async def test_mysql_security_transactions(db_session_factory, monkeypatch):
    monkeypatch.setattr(settings, "auth_signing_key", SecretStr("isolated-mysql-test-signing-key-12345"))
    monkeypatch.setattr(settings, "auth_rate_limit", 2)
    limited = await asyncio.gather(*(auth_service.throttle(
        "isolated-test-ip", "mysql-user") for _ in range(3)), return_exceptions=True)
    assert sum(result is None for result in limited) == 2
    assert sum(isinstance(result, SecurityError) and result.status_code == 429 for result in limited) == 1
    await auth_service.purge_expired_counters()
    user = await auth_service.create_user("mysql-user", "Correct-password-123")
    cid = await repository.create_conversation(user.user_id)
    cid2 = await repository.create_conversation(user.user_id)
    async with db_session_factory() as s, s.begin():
        s.add(Order(order_id="1001", user_id=user.user_id, status="已付款",
                    amount=Decimal("100.00"), product="test", tracking_no="SF1001",
                    refundable=True, refund_deadline=now_utc() + timedelta(days=7)))
    access, raw = await auth_service.login("mysql-user", "Correct-password-123")
    assert (await auth_service.authenticated_principal(verify_access(access))).user_id == user.user_id
    rotated = await asyncio.gather(auth_service.rotate(raw), auth_service.rotate(raw), return_exceptions=True)
    assert sum(isinstance(r, tuple) for r in rotated) == 1
    assert sum(isinstance(r, SecurityError) for r in rotated) == 1
    winner = next(r for r in rotated if isinstance(r, tuple))
    with pytest.raises(SecurityError):
        await auth_service.authenticated_principal(verify_access(winner[0]))

    args = {"description": "MySQL transaction test", "ticket_type": "咨询"}
    grant = await tickets.prepare(user.user_id, cid, "create_ticket", args)
    async def ticket():
        return await tickets.submit(user_id=user.user_id, conversation_id=cid,
            kind="create_ticket", args=args, approval_id=grant["approval_id"])
    first, again = await asyncio.gather(ticket(), ticket())
    assert first == again

    refund_args = {"order_id": "1001", "reason": "其他"}
    a = await tickets.prepare(user.user_id, cid, "create_refund", refund_args)
    b = await tickets.prepare(user.user_id, cid2, "create_refund", refund_args)
    async def refund(conversation_id, approval):
        return await submit_refund_request(user_id=user.user_id, conversation_id=conversation_id,
                                          **refund_args, approval_id=approval["approval_id"])
    results = await asyncio.gather(refund(cid, a), refund(cid2, b), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, SecurityError) for r in results) == 1
    async with db_session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 2
        assert await s.scalar(select(func.count()).select_from(BusinessOperation)) == 2
        assert await s.scalar(select(func.count()).select_from(RefundRequest)) == 1

    access, raw = await auth_service.login("mysql-user", "Correct-password-123")
    await auth_service.logout(raw)
    with pytest.raises(SecurityError):
        await auth_service.authenticated_principal(verify_access(access))
