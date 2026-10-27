from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import func, select

from app.db import repository
from app.db.models import Message, Ticket, User
from app.graph import runtime
from app.security.errors import SecurityError
from app.services import auth_service
from app.tools import business


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/conversations", None),
    ("GET", "/api/conversations/1/messages", None),
    ("POST", "/api/agent", {"message": "hello"}),
    ("POST", "/api/chat", {"message": "hello"}),
    ("POST", "/api/actions/prepare", {"conversation_id": 1, "tool_name": "create_ticket",
                                     "arguments": {"description": "test", "ticket_type": "咨询"}}),
    ("POST", "/api/actions/resume", {"conversation_id": 1, "order_id": "1001"}),
    ("POST", "/api/feedback", {"conversation_id": 1, "rating": "down", "question": "test"}),
])
async def test_no_anonymous_access(api, method, path, body):
    r = await api.request(method, path, json=body) if body else await api.request(method, path)
    assert r.status_code == 401


async def test_conversation_list_ignores_forged_query_identity(api, alice_headers):
    r = await api.get("/api/conversations?user_id=bob", headers=alice_headers)
    assert [item["id"] for item in r.json()["items"]] == [1]


async def test_cross_user_history_chat_feedback_and_resume_rejected_before_stream(
        api, alice_headers, monkeypatch, secure_db):
    graph = AsyncMock()
    monkeypatch.setattr(runtime, "get_graph", lambda: graph)
    assert (await api.get("/api/conversations/2/messages", headers=alice_headers)).status_code == 404
    for path, body in [
        ("/api/chat", {"message": "hello", "conversation_id": 2}),
        ("/api/agent", {"message": "hello", "conversation_id": 2}),
        ("/api/actions/resume", {"conversation_id": 2, "order_id": "2002"}),
        ("/api/feedback", {"conversation_id": 2, "rating": "down", "question": "test"}),
    ]:
        r = await api.post(path, json=body, headers=alice_headers)
        assert r.status_code == 404, (path, r.text)
    graph.aget_state.assert_not_called()
    graph.ainvoke.assert_not_called()
    async with secure_db() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 0
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 0


@pytest.mark.parametrize("path", ["/api/agent", "/api/chat"])
async def test_body_cannot_choose_identity(api, alice_headers, path):
    r = await api.post(path, json={"message": "hi", "user_id": "bob"}, headers=alice_headers)
    assert r.status_code == 422


@pytest.mark.parametrize("mode", ["run", "stream", "resume", "stream_resume", "snapshot"])
async def test_runtime_owner_check_precedes_graph_access(secure_db, monkeypatch, mode):
    graph = AsyncMock()
    monkeypatch.setattr(runtime, "get_graph", lambda: graph)
    with pytest.raises(runtime.ConversationNotFound):
        if mode == "run":
            await runtime.run_turn("alice", "hi", 2)
        elif mode == "stream":
            _ = [ev async for ev in runtime.stream_turn("alice", "hi", 2)]
        elif mode == "resume":
            await runtime.resume_turn("alice", 2, "2002")
        elif mode == "stream_resume":
            _ = [ev async for ev in runtime.stream_resume("alice", 2, "2002")]
        else:
            await runtime.get_turn_snapshot("alice", 2)
    graph.aget_state.assert_not_called()
    graph.ainvoke.assert_not_called()


async def test_order_facts_are_user_scoped(secure_db):
    assert await business.owns_order("alice", "1001")
    assert not await business.owns_order("bob", "1001")
    assert not await business.owns_order("alice", "9999")
    assert [o["order_id"] for o in await business.list_user_orders("bob")] == ["2002"]


async def test_actual_turn_injects_authenticated_identity(api, alice_headers, secure_db, monkeypatch):
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage("hello")]}
    monkeypatch.setattr(runtime, "get_graph", lambda: graph)
    monkeypatch.setattr(runtime.summarizer, "maybe_schedule_summary", AsyncMock())
    r = await api.post("/api/agent", json={"message": "hello"}, headers=alice_headers)
    assert r.status_code == 200
    state = graph.ainvoke.call_args.args[0]
    assert state["user_id"] == "alice" and state["run_id"]
    conv = await repository.get_conversation(r.json()["conversation_id"])
    assert conv.user_id == "alice"


async def test_resume_checks_actual_pending_interrupt(api, alice_headers, monkeypatch):
    graph = AsyncMock()
    graph.aget_state.return_value = SimpleNamespace(values={"user_id": "alice"}, tasks=(), interrupts=())
    monkeypatch.setattr(runtime, "get_graph", lambda: graph)
    r = await api.post("/api/actions/resume", json={"conversation_id": 1, "order_id": "1001"}, headers=alice_headers)
    assert r.status_code == 409
    graph.ainvoke.assert_not_called()


async def test_admin_role_required(api, alice_headers):
    assert (await api.get("/api/admin/probe")).status_code == 401
    assert (await api.get("/api/admin/probe", headers=alice_headers)).status_code == 403
    token, _ = await auth_service.login("admin", "Correct-password-123")
    assert (await api.get("/api/admin/probe", headers={"Authorization": f"Bearer {token}"})).status_code == 200


async def test_main_admin_routes_have_role_dependency(secure_db):
    from app.main import app
    from app.api.deps import require_admin
    protected = {"/api/admin", "/api/kb", "/api/review", "/api/jobs", "/api/observability",
                 "/api/topics", "/api/acceptance", "/api/rag-eval", "/api/extract"}
    for route in app.routes:
        if any(getattr(route, "path", "").startswith(prefix) for prefix in protected):
            assert require_admin in [d.call for d in route.dependant.dependencies], route.path

