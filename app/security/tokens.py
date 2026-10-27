import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings
from app.security.errors import SecurityError


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def signing_key() -> str:
    key = settings.auth_signing_key.get_secret_value()
    if len(key.encode()) < 32:
        raise SecurityError("auth_signing_key_not_configured", 503)
    return key


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_token() -> str:
    return secrets.token_urlsafe(48)


def issue_access(user_id: str, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": user_id, "sid": session_id, "jti": secrets.token_hex(16),
        "iat": now, "exp": now + timedelta(seconds=settings.auth_access_ttl_seconds),
        "iss": settings.auth_issuer, "aud": settings.auth_audience,
        "token_type": "access",
    }, signing_key(), algorithm="HS256")


def verify_access(token: str) -> dict:
    try:
        claims = jwt.decode(token, signing_key(), algorithms=["HS256"],
                            issuer=settings.auth_issuer, audience=settings.auth_audience,
                            options={"require": ["sub", "sid", "jti", "iat", "exp", "iss", "aud", "token_type"]})
        if claims["token_type"] != "access" or not all(
                isinstance(claims[k], str) and claims[k] for k in ("sub", "sid", "jti")):
            raise ValueError("invalid claims")
        if claims["exp"] <= claims["iat"]:
            raise ValueError("invalid lifetime")
        return claims
    except (jwt.PyJWTError, ValueError, TypeError):
        raise SecurityError("authentication_failed", 401) from None
