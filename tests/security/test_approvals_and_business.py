import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.db.models import Approval, BusinessOperation, Conversation, Order, RefundRequest, Ticket
from app.security import approvals
from app.security.context import SecurityContext
from app.security.errors import SecurityError
from app.security.principal import Principal
from app.security.tokens import now_utc
from app.services import tickets
from app.services.refunds import submit_refund_request

ARGS = {"description": "需要人工跟进", "ticket_type": "咨询"}


async def count(factory, model):
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def test_no_approval_no_effect(api, alice_headers, secure_db):
    r = await api.post("/api/actions/create-ticket", json={"conversation_id": 1, **ARGS}, headers=alice_headers)
    assert r.status_code == 422 and await count(secure_db, Ticket) == 0


@pytest.mark.parametrize("conversation,order", [(2, "2002"), (1, "2002"), (2, "1001"), (1, "9999")])
async def test_joint_owner_authorization(api, alice_headers, secure_db, conversation, order):
    args = {"order_id": order, "reason": "其他"}
    r = await api.post("/api/actions/prepare", json={"conversation_id": conversation,
        "tool_name": "create_refund", "arguments": args}, headers=alice_headers)
    assert r.status_code == 404
    assert await count(secure_db, Approval) == await count(secure_db, Ticket) == 0
    r = await api.post("/api/actions/create-refund", json={
        "conversation_id": conversation, **args, "approval_id": "0" * 32}, headers=alice_headers)
    assert r.status_code == 404
    assert await count(secure_db, Ticket) == 0


async def test_prepare_exact_preview_and_http_idempotency(api, alice_headers, secure_db):
    r = await api.post("/api/actions/prepare", json={"conversation_id": 1,
        "tool_name": "create_ticket", "arguments": ARGS}, headers=alice_headers)
    assert r.status_code == 200 and r.json()["preview"] == ARGS
    body = {"conversation_id": 1, **ARGS, "approval_id": r.json()["approval_id"]}
    first = await api.post("/api/actions/create-ticket", json=body, headers=alice_headers)
    again = await api.post("/api/actions/create-ticket", json=body, headers=alice_headers)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert await count(secure_db, Ticket) == await count(secure_db, BusinessOperation) == 1


async def test_altered_arguments_and_user_rejected(api, alice_headers, bob_headers, secure_db):
    grant = await tickets.prepare("alice", 1, "create_ticket", ARGS)
    for headers, body in [
        (alice_headers, {"conversation_id": 1, **ARGS, "description": "替换描述"}),
        (bob_headers, {"conversation_id": 2, **ARGS}),
    ]:
        r = await api.post("/api/actions/create-ticket",
                           json={**body, "approval_id": grant["approval_id"]}, headers=headers)
        assert r.status_code == 403
    assert await count(secure_db, Ticket) == 0


@pytest.mark.parametrize("change", ["run", "toolcall", "user", "conversation", "tool", "identity", "args", "expiry", "policy"])
async def test_approval_scope_and_expiry(secure_db, change):
    ctx = SecurityContext(Principal("alice"), 1, "run1", "call1")
    identity = tickets.identity("create_ticket")
    grant = await approvals.issue(ctx, "create_ticket", identity, ARGS)
    name, args, ident = "create_ticket", ARGS, identity
    if change == "run": ctx = SecurityContext(Principal("alice"), 1, "run2", "call1")
    if change == "toolcall": ctx = SecurityContext(Principal("alice"), 1, "run1", "call2")
    if change == "user": ctx = SecurityContext(Principal("bob"), 1, "run1", "call1")
    if change == "conversation": ctx = SecurityContext(Principal("alice"), 2, "run1", "call1")
    if change == "tool": name = "create_refund"
    if change == "identity": ident = "mcp:untrusted:create_ticket"
    if change == "args": args = {**ARGS, "description": "changed"}
    if change in {"expiry", "policy"}:
        async with secure_db() as s, s.begin():
            row = await s.get(Approval, grant.approval_id)
            if change == "expiry": row.expires_at = now_utc() - timedelta(seconds=1)
            else: row.policy_revision = "old"
    with pytest.raises(SecurityError):
        await approvals.check(grant.approval_id, ctx, name, ident, args)
    assert await count(secure_db, Ticket) == 0


async def test_refund_eligibility_and_duplicate_order(api, alice_headers, secure_db):
    async with secure_db() as s, s.begin():
        (await s.get(Order, "1001")).refundable = False
    args = {"order_id": "1001", "reason": "其他"}
    r = await api.post("/api/actions/prepare", json={"conversation_id": 1,
        "tool_name": "create_refund", "arguments": args}, headers=alice_headers)
    assert r.status_code == 409 and await count(secure_db, Ticket) == 0
    async with secure_db() as s, s.begin():
        (await s.get(Order, "1001")).refundable = True
    grant = await tickets.prepare("alice", 1, "create_refund", args)
    result = await submit_refund_request(user_id="alice", conversation_id=1, **args, approval_id=grant["approval_id"])
    assert result.startswith("T") and await count(secure_db, RefundRequest) == 1
    grant2 = await tickets.prepare("alice", 1, "create_refund", args)
    with pytest.raises(SecurityError, match="refund_already_requested"):
        await submit_refund_request(user_id="alice", conversation_id=1, **args, approval_id=grant2["approval_id"])
    assert await count(secure_db, Ticket) == 1


async def test_business_policy_rechecked_after_preview(secure_db):
    args = {"order_id": "1001", "reason": "其他"}
    grant = await tickets.prepare("alice", 1, "create_refund", args)
    async with secure_db() as s, s.begin():
        (await s.get(Order, "1001")).status = "待付款"
    with pytest.raises(SecurityError):
        await submit_refund_request(user_id="alice", conversation_id=1, **args, approval_id=grant["approval_id"])
    assert await count(secure_db, Ticket) == 0
    async with secure_db() as s:
        assert (await s.get(Approval, grant["approval_id"])).consumed_at is None


async def test_partial_write_failure_rolls_back_ticket_status_and_grant(secure_db, monkeypatch):
    grant = await tickets.prepare("alice", 1, "create_ticket", ARGS)
    def fail(*a, **kw):
        raise RuntimeError("audit_failure")
    monkeypatch.setattr(approvals, "audit_row", fail)
    with pytest.raises(RuntimeError):
        await tickets.submit(user_id="alice", conversation_id=1, kind="create_ticket", args=ARGS,
                             approval_id=grant["approval_id"])
    assert await count(secure_db, Ticket) == await count(secure_db, BusinessOperation) == 0
    async with secure_db() as s:
        assert (await s.get(Approval, grant["approval_id"])).consumed_at is None
        assert (await s.get(Conversation, 1)).status == "进行中"


async def test_concurrent_operation_at_most_one_effect(secure_db):
    grant = await tickets.prepare("alice", 1, "create_ticket", ARGS)
    async def submit():
        return await tickets.submit(user_id="alice", conversation_id=1, kind="create_ticket",
                                    args=ARGS, approval_id=grant["approval_id"])
    results = await asyncio.gather(submit(), submit())
    assert results[0] == results[1]
    assert await count(secure_db, Ticket) == 1


async def test_concurrent_refund_different_grants_one_request(secure_db):
    args = {"order_id": "1001", "reason": "其他"}
    a = await tickets.prepare("alice", 1, "create_refund", args)
    b = await tickets.prepare("alice", 1, "create_refund", args)
    async def submit(grant):
        return await submit_refund_request(user_id="alice", conversation_id=1, **args, approval_id=grant["approval_id"])
    results = await asyncio.gather(submit(a), submit(b), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, SecurityError) for r in results) == 1
    assert await count(secure_db, RefundRequest) == await count(secure_db, Ticket) == 1



async def test_operation_status_is_owned_and_available_after_completion(api, alice_headers, bob_headers):
    grant = await tickets.prepare("alice", 1, "create_ticket", ARGS)
    path = "/api/actions/operations/" + grant["approval_id"]
    assert (await api.get(path, headers=alice_headers)).json()["state"] == "pending_or_not_executed"
    assert (await api.get(path, headers=bob_headers)).status_code == 404
    number = await tickets.submit(user_id="alice", conversation_id=1, kind="create_ticket",
                                 args=ARGS, approval_id=grant["approval_id"])
    r = await api.get(path, headers=alice_headers)
    assert r.json()["state"] == "completed" and r.json()["ticket_no"] == number


async def test_plan_mode_cannot_be_bypassed_through_http_forms(api, alice_headers, secure_db, monkeypatch):
    from app.config import settings
    grant = await tickets.prepare("alice", 1, "create_ticket", ARGS)
    monkeypatch.setattr(settings, "tool_security_mode", "plan")
    response = await api.post("/api/actions/create-ticket", headers=alice_headers,
        json={"conversation_id": 1, **ARGS, "approval_id": grant["approval_id"]})
    assert response.status_code == 403 and await count(secure_db, Ticket) == 0
    response = await api.post("/api/actions/prepare", headers=alice_headers,
        json={"conversation_id": 1, "tool_name": "create_ticket", "arguments": ARGS})
    assert response.status_code == 403
