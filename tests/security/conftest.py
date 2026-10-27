from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
import app.db.base as db
from app.db.models import Conversation, Order, User
from app.security.errors import SecurityError
from app.security.passwords import hash_password
from app.security.tokens import now_utc


@pytest.fixture(scope="session")
def encoded_password():
    return hash_password("Correct-password-123")


@pytest_asyncio.fixture
async def secure_db(tmp_path, monkeypatch, encoded_password):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'security.sqlite'}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(conn, record):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "async_session", factory)
    monkeypatch.setattr(settings, "auth_signing_key", SecretStr("test-signing-secret-32-bytes-long-12345"))
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_registration_enabled", False)
    monkeypatch.setattr(settings, "auth_rate_limit", 10)
    monkeypatch.setattr(settings, "tool_security_mode", "default")
    monkeypatch.setattr(settings, "tool_max_retries", 0)
    monkeypatch.setattr(settings, "mcp_schema_pins", {})
    monkeypatch.setattr(settings, "mcp_allow_local_http", False)
    async with factory() as s, s.begin():
        s.add_all([User(user_id=uid, username=name, password_hash=encoded_password,
                        role=role, is_active=True) for uid, name, role in
                   (("alice", "alice", "user"), ("bob", "bob", "user"), ("admin", "admin", "admin"))])
        await s.flush()
        s.add_all([Conversation(id=1, user_id="alice"), Conversation(id=2, user_id="bob")])
        s.add_all([Order(order_id=oid, user_id=uid, status="已付款", amount=Decimal("100.00"),
                         product="test product", tracking_no=tracking, refundable=True,
                         refund_deadline=now_utc() + timedelta(days=7))
                   for oid, uid, tracking in (("1001", "alice", "SF1001"), ("2002", "bob", "SF2002"))])
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def api(secure_db):
    # Real routers and auth dependencies, with no LLM/Milvus/MCP startup.
    from app.api import actions, agent, auth, chat, conversations, feedback
    from app.api.deps import require_admin
    app = FastAPI()
    for module in (actions, agent, auth, chat, conversations, feedback):
        app.include_router(module.router)
    from fastapi import Depends
    @app.get("/api/admin/probe", dependencies=[Depends(require_admin)])
    async def admin_probe():
        return {"ok": True}
    @app.exception_handler(SecurityError)
    async def security_error(request, exc):
        return JSONResponse({"detail": exc.code}, status_code=exc.status_code)
    from sqlalchemy.exc import SQLAlchemyError
    @app.exception_handler(SQLAlchemyError)
    async def db_error(request, exc):
        return JSONResponse({"detail": "service_unavailable"}, status_code=503)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        yield client


@pytest_asyncio.fixture
async def alice_headers(secure_db):
    from app.services.auth_service import login
    access, _ = await login("alice", "Correct-password-123")
    return {"Authorization": f"Bearer {access}"}


@pytest_asyncio.fixture
async def bob_headers(secure_db):
    from app.services.auth_service import login
    access, _ = await login("bob", "Correct-password-123")
    return {"Authorization": f"Bearer {access}"}

