from app.api import actions


def test_create_ticket_action_calls_authorized_service(client, monkeypatch):
    async def fake_submit(**kw):
        assert kw["user_id"] == "u1" and kw["conversation_id"] == 5
        assert kw["args"]["ticket_type"] == "投诉" and kw["approval_id"] == "a" * 32
        return "T20260715001"
    monkeypatch.setattr(actions.tickets, "submit", fake_submit)
    r = client.post("/api/actions/create-ticket", json={
        "conversation_id": 5, "description": "东西坏了", "ticket_type": "投诉", "approval_id": "a" * 32})
    assert r.status_code == 200 and r.json()["ticket_no"] == "T20260715001"


def test_create_ticket_action_rejects_bad_type(client):
    r = client.post("/api/actions/create-ticket", json={
        "conversation_id": 5, "description": "x", "ticket_type": "乱填", "approval_id": "a" * 32})
    assert r.status_code == 422
