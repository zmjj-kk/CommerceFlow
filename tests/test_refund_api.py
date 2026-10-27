from unittest.mock import AsyncMock
import pytest
from app.api import actions


@pytest.fixture(autouse=True)
def resume_preflight(monkeypatch):
    monkeypatch.setattr(actions, "require_conversation", AsyncMock())
    monkeypatch.setattr(actions.runtime, "validate_resume", AsyncMock())


def test_create_refund_calls_authorized_service(client, monkeypatch):
    async def fake_create(**kw):
        assert kw["user_id"] == "u1" and kw["conversation_id"] == 7
        assert kw["order_id"] == "1001" and kw["approval_id"] == "a" * 32
        return "T20260716001"
    monkeypatch.setattr(actions, "submit_refund_request", fake_create)
    r = client.post("/api/actions/create-refund", json={
        "conversation_id": 7, "order_id": "1001", "reason": "质量问题", "approval_id": "a" * 32})
    assert r.status_code == 200 and r.json()["status"] == "退款申请已提交"


def test_create_refund_rejects_bad_reason(client):
    r = client.post("/api/actions/create-refund", json={
        "conversation_id": 7, "order_id": "1001", "reason": "乱填", "approval_id": "a" * 32})
    assert r.status_code == 422


def test_resume_endpoint_streams(client, monkeypatch):
    async def fake_stream_resume(uid, cid, value):
        assert uid == "u1" and cid == 3 and value == "1001"
        yield {"type": "delta", "text": "可以提交退款申请"}
        yield {"type": "actions", "items": [{"type": "refund_form", "draft": {"order_id": "1001"}}]}
        yield {"type": "done", "conversation_id": 3}
    monkeypatch.setattr(actions.runtime, "stream_resume", fake_stream_resume)
    r = client.post("/api/actions/resume", json={"conversation_id": 3, "order_id": "1001"})
    assert r.status_code == 200 and "refund_form" in r.text and "[DONE]" in r.text


def test_resume_endpoint_confirmed_branch(client, monkeypatch):
    async def fake_stream_resume(uid, cid, value):
        assert value == {"confirmed": False, "approval_id": None}
        yield {"type": "delta", "text": "已取消建单"}
        yield {"type": "done", "conversation_id": cid}
    monkeypatch.setattr(actions.runtime, "stream_resume", fake_stream_resume)
    r = client.post("/api/actions/resume", json={"conversation_id": 3, "confirmed": False})
    assert r.status_code == 200 and "已取消建单" in r.text


def test_resume_requires_one_value_and_bound_approval(client):
    for body in ({"conversation_id": 3}, {"conversation_id": 3, "confirmed": True},
                 {"conversation_id": 3, "confirmed": False, "order_id": "1001"}):
        assert client.post("/api/actions/resume", json=body).status_code == 422
