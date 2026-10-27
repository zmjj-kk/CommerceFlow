import asyncio
from datetime import timedelta
import json

import jwt
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import AuthFamily, AuthSession, User
from app.security.errors import SecurityError
from app.security.passwords import hash_password, verify_password
from app.security.tokens import now_utc, verify_access
from app.services import auth_service

PASSWORD = "Correct-password-123"


async def test_closed_registration_and_strict_identity(api, secure_db):
    r = await api.post("/api/auth/register", json={"username": "carol", "password": PASSWORD})
    assert r.status_code == 403
    r = await api.post("/api/auth/register", json={"username": "carol", "password": PASSWORD,
                                                  "role": "admin", "user_id": "alice"})
    assert r.status_code == 422
    async with secure_db() as s:
        assert len(list((await s.execute(select(User))).scalars())) == 3


async def test_registration_hash_normalization_and_weak_password(api, secure_db, monkeypatch):
    monkeypatch.setattr(settings, "auth_registration_enabled", True)
    assert (await api.post("/api/auth/register", json={"username": "carol", "password": "weak"})).status_code == 422
    r = await api.post("/api/auth/register", json={"username": "  CAROL ", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["role"] == "user"
    assert r.json()["user_id"] != "carol" and "password" not in r.text
    assert (await api.post("/api/auth/register", json={"username": "carol", "password": PASSWORD})).status_code == 409
    async with secure_db() as s:
        user = (await s.execute(select(User).where(User.username == "carol"))).scalar_one()
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(PASSWORD, user.password_hash)


async def test_login_rotation_replay_and_logout(api, secure_db):
    r = await api.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200 and "refresh_token" not in r.json()
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/auth" in cookie
    assert r.headers["cache-control"] == "no-store"
    old_cookie = api.cookies.get("commerceflow_refresh")
    access = r.json()["access_token"]
    assert (await api.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})).json()["user_id"] == "alice"
    r = await api.post("/api/auth/refresh")
    assert r.status_code == 200 and old_cookie != api.cookies.get("commerceflow_refresh")
    new_access = r.json()["access_token"]
    # Only hashes are retained, including rotated tokens for replay detection.
    async with secure_db() as s:
        sessions = list((await s.execute(select(AuthSession))).scalars())
        assert len(sessions) == 2
        assert all(old_cookie != row.refresh_token_hash for row in sessions)
        assert any(row.used_at for row in sessions)
    assert (await api.post("/api/auth/refresh", headers={"Cookie": f"commerceflow_refresh={old_cookie}"})).status_code == 401
    assert (await api.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})).status_code == 401
    assert (await api.post("/api/auth/refresh")).status_code == 401
    r = await api.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    token = r.json()["access_token"]
    assert (await api.post("/api/auth/logout")).status_code == 204
    assert (await api.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    assert (await api.post("/api/auth/refresh")).status_code == 401


async def test_uniform_login_failure_disabled_user_and_rate_limit(api, secure_db, monkeypatch):
    a = await api.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD})
    b = await api.post("/api/auth/login", json={"username": "alice", "password": "wrong-password-123"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    async with secure_db() as s, s.begin():
        (await s.get(User, "alice")).is_active = False
    assert (await api.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})).status_code == 401
    monkeypatch.setattr(settings, "auth_rate_limit", 3)
    assert (await api.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})).status_code == 429


@pytest.mark.parametrize("claim,value", [
    ("iss", "evil"), ("aud", "other"), ("token_type", "refresh"),
    ("exp", 1), ("iat", 9999999999), ("sub", ""), ("sid", ""),
])
async def test_invalid_claims_rejected(api, alice_headers, claim, value):
    token = alice_headers["Authorization"].split()[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    claims[claim] = value
    bad = jwt.encode(claims, settings.auth_signing_key.get_secret_value(), algorithm="HS256")
    assert (await api.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})).status_code == 401


async def test_tamper_signature_missing_claim_and_disabled_user(api, alice_headers, secure_db):
    original = alice_headers["Authorization"].split()[1]
    claims = jwt.decode(original, options={"verify_signature": False})
    for bad in (jwt.encode(claims, "another-secret-that-is-at-least-32-bytes", algorithm="HS256"), "malformed"):
        assert (await api.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})).status_code == 401
    del claims["jti"]
    bad = jwt.encode(claims, settings.auth_signing_key.get_secret_value(), algorithm="HS256")
    assert (await api.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})).status_code == 401
    async with secure_db() as s, s.begin():
        (await s.get(User, "alice")).is_active = False
    assert (await api.get("/api/auth/me", headers=alice_headers)).status_code == 401


async def test_csrf_origin_on_refresh_logout_and_login(api):
    await api.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    for path in ("/api/auth/refresh", "/api/auth/logout"):
        assert (await api.post(path, headers={"Origin": "https://evil.example"})).status_code == 403
        request = api.build_request("POST", path)
        del request.headers["origin"]
        assert (await api.send(request)).status_code == 403
    assert (await api.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                           headers={"Origin": "https://evil.example"})).status_code == 403


async def test_concurrent_refresh_has_one_winner_and_revokes_family(secure_db):
    _, raw = await auth_service.login("alice", PASSWORD)
    results = await asyncio.gather(auth_service.rotate(raw), auth_service.rotate(raw), return_exceptions=True)
    assert sum(isinstance(r, tuple) for r in results) == 1
    assert sum(isinstance(r, SecurityError) for r in results) == 1
    access = next(r[0] for r in results if isinstance(r, tuple))
    with pytest.raises(SecurityError):
        await auth_service.authenticated_principal(verify_access(access))


async def test_auth_logs_do_not_contain_credentials(api, caplog):
    r = await api.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    token = r.json()["access_token"]
    assert PASSWORD not in caplog.text and token not in caplog.text
    assert api.cookies.get("commerceflow_refresh") not in caplog.text



async def test_expired_rate_counters_are_purged_without_removing_current(secure_db):
    from app.db.models import AuthThrottle
    async with secure_db() as s, s.begin():
        s.add(AuthThrottle(key="expired", count=1, expires_at=now_utc() - timedelta(seconds=1)))
        s.add(AuthThrottle(key="current", count=1, expires_at=now_utc() + timedelta(seconds=300)))
    await auth_service.purge_expired_counters()
    async with secure_db() as s:
        assert await s.get(AuthThrottle, "expired") is None
        assert await s.get(AuthThrottle, "current") is not None
