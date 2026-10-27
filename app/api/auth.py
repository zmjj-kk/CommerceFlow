from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from app.api.deps import get_current_user
from app.config import settings
import app.db.base as db
from app.db.models import User
from app.schemas.auth import Credentials, TokenResponse, UserView
from app.security.principal import Principal
from app.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE = "commerceflow_refresh"


def check_origin(request: Request, *, required: bool = True):
    origin = request.headers.get("origin")
    allowed = settings.auth_allowed_origins or [str(request.base_url).rstrip("/")]
    if (required or origin) and (not origin or origin not in allowed):
        raise HTTPException(403, "origin_not_allowed")


def tokens(response: Response, access: str, refresh: str) -> TokenResponse:
    response.set_cookie(COOKIE, refresh, httponly=True, secure=settings.auth_cookie_secure,
                        samesite="strict", max_age=settings.auth_refresh_ttl_seconds, path="/api/auth")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(access_token=access, expires_in=settings.auth_access_ttl_seconds)


def peer(request: Request) -> str:
    # Do not trust client-supplied X-Forwarded-For; configure the ASGI trusted proxy explicitly.
    return request.client.host if request.client else "unknown"


@router.post("/register", response_model=UserView, status_code=201)
async def register(req: Credentials, request: Request):
    check_origin(request, required=False)
    if not settings.auth_registration_enabled:
        raise HTTPException(403, "public_registration_disabled")
    await auth_service.throttle(peer(request), req.username.casefold().strip(), "register")
    user = await auth_service.create_user(req.username, req.password.get_secret_value())
    return UserView(user_id=user.user_id, username=user.username, role=user.role)


@router.post("/login", response_model=TokenResponse)
async def login(req: Credentials, request: Request, response: Response):
    check_origin(request, required=False)
    from app.security.passwords import normalize_username
    from app.security.errors import SecurityError
    try:
        username = normalize_username(req.username)
    except SecurityError:
        username = ""
    await auth_service.throttle(peer(request), username)
    access, refresh = await auth_service.login(req.username, req.password.get_secret_value())
    return tokens(response, access, refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, response: Response):
    check_origin(request)
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(401, "authentication_failed")
    access, replacement = await auth_service.rotate(raw)
    return tokens(response, access, replacement)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    check_origin(request)
    await auth_service.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/api/auth", secure=settings.auth_cookie_secure,
                           httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"


@router.get("/me", response_model=UserView)
async def me(response: Response, user: Principal = Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    async with db.async_session() as s:
        row = await s.get(User, user.user_id)
        return UserView(user_id=row.user_id, username=row.username, role=row.role)
