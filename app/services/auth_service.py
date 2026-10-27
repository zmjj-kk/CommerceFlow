from datetime import timedelta
import hashlib
import time
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.config import settings
import app.db.base as db
from app.db.models import AuthFamily, AuthSession, AuthThrottle, User
from app.security.errors import SecurityError
from app.security.passwords import dummy_hash, hash_password, normalize_username, verify_password
from app.security.tokens import hash_token, issue_access, now_utc, refresh_token, signing_key
from app.security.transactions import transaction


async def throttle(ip: str, username: str, purpose: str = "login"):
    now = now_utc()
    window = settings.auth_rate_window_seconds
    bucket = int(time.time()) // window
    keys = [hashlib.sha256(f"{purpose}:{kind}:{value}:{bucket}".encode()).hexdigest()
            for kind, value in (("ip", ip), ("user", username))]
    blocked = False
    async with transaction() as s:
        dialect = s.bind.dialect.name
        for key in keys:
            values = dict(key=key, count=1, expires_at=now + timedelta(seconds=window * 2))
            if dialect == "mysql":
                from sqlalchemy.dialects.mysql import insert
                stmt = insert(AuthThrottle).values(**values)
                stmt = stmt.on_duplicate_key_update(count=AuthThrottle.count + 1)
            else:
                from sqlalchemy.dialects.sqlite import insert
                stmt = insert(AuthThrottle).values(**values).on_conflict_do_update(
                    index_elements=["key"], set_={"count": AuthThrottle.count + 1})
            await s.execute(stmt)
            count = (await s.execute(select(AuthThrottle.count).where(AuthThrottle.key == key))).scalar_one()
            blocked |= count > settings.auth_rate_limit
    if blocked:
        raise SecurityError("too_many_attempts", 429)


async def create_user(username: str, password: str, *, role: str = "user") -> User:
    username = normalize_username(username)
    if role not in {"user", "admin"}:
        raise SecurityError("invalid_role", 422)
    encoded = await run_in_threadpool(hash_password, password)
    user = User(user_id=uuid4().hex, username=username, password_hash=encoded, role=role, is_active=True)
    try:
        async with transaction() as s:
            s.add(user)
            await s.flush()
    except IntegrityError:
        raise SecurityError("username_unavailable", 409) from None
    return user


def _session(user_id: str, family: AuthFamily):
    raw = refresh_token()
    session = AuthSession(id=uuid4().hex, user_id=user_id, family_id=family.id,
                          refresh_token_hash=hash_token(raw), expires_at=family.expires_at)
    return session, raw


async def login(username: str, password: str):
    signing_key()  # fail before creating any session when configuration is absent
    try:
        username = normalize_username(username)
    except SecurityError:
        username = ""
    async with transaction() as s:
        user = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        encoded = user.password_hash if user else await run_in_threadpool(dummy_hash)
        valid = await run_in_threadpool(verify_password, password, encoded)
        if not valid or not user or not user.is_active:
            raise SecurityError("invalid_credentials", 401)
        family = AuthFamily(id=uuid4().hex, user_id=user.user_id,
                            expires_at=now_utc() + timedelta(seconds=settings.auth_refresh_ttl_seconds))
        session, raw = _session(user.user_id, family)
        s.add(family)
        await s.flush()
        s.add(session)
        access = issue_access(user.user_id, session.id)
    return access, raw


async def rotate(raw: str):
    signing_key()
    now = now_utc()
    failed = False
    access = replacement = None
    async with transaction() as s:
        old = (await s.execute(select(AuthSession).where(
            AuthSession.refresh_token_hash == hash_token(raw)))).scalar_one_or_none()
        if old is None:
            raise SecurityError("authentication_failed", 401)
        family = (await s.execute(select(AuthFamily).where(
            AuthFamily.id == old.family_id).with_for_update())).scalar_one()
        user = await s.get(User, old.user_id)
        # Atomic compare-and-set also protects SQLite (which ignores FOR UPDATE).
        changed = await s.execute(update(AuthSession).where(
            AuthSession.id == old.id, AuthSession.used_at.is_(None),
            AuthSession.expires_at > now).values(used_at=now))
        if changed.rowcount != 1 or family.revoked_at or family.expires_at <= now or not user or not user.is_active:
            family.revoked_at = now
            failed = True
        else:
            new, replacement = _session(user.user_id, family)
            s.add(new)
            access = issue_access(user.user_id, new.id)
    # Commit family revocation before returning the error.
    if failed:
        raise SecurityError("authentication_failed", 401)
    return access, replacement


async def logout(raw: str | None, session_id: str | None = None):
    async with transaction() as s:
        q = select(AuthSession)
        if raw:
            q = q.where(AuthSession.refresh_token_hash == hash_token(raw))
        elif session_id:
            q = q.where(AuthSession.id == session_id)
        else:
            return
        session = (await s.execute(q)).scalar_one_or_none()
        if session:
            await s.execute(update(AuthFamily).where(AuthFamily.id == session.family_id).values(revoked_at=now_utc()))


async def authenticated_principal(claims: dict):
    from app.security.principal import Principal
    async with db.async_session() as s:
        row = (await s.execute(select(User, AuthFamily, AuthSession)
            .join(AuthSession, User.user_id == AuthSession.user_id)
            .join(AuthFamily, AuthFamily.id == AuthSession.family_id)
            .where(User.user_id == claims["sub"], AuthSession.id == claims["sid"]))).first()
        if not row or not row[0].is_active or row[1].revoked_at or row[1].expires_at <= now_utc():
            raise SecurityError("authentication_failed", 401)
        return Principal(user_id=row[0].user_id, role=row[0].role, session_id=row[2].id)


async def purge_expired_counters() -> None:
    """Keep range cleanup out of counter upsert transactions to avoid MySQL gap-lock cycles."""
    async with db.async_session() as s:
        if s.bind.dialect.name == "mysql":
            await s.connection(execution_options={"isolation_level": "READ COMMITTED"})
        await s.execute(delete(AuthThrottle).where(AuthThrottle.expires_at < now_utc()))
        await s.commit()
