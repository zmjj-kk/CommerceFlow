from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import repository
from app.security.errors import SecurityError
from app.security.principal import Principal, CurrentUser
from app.security.tokens import verify_access
from app.services.auth_service import authenticated_principal

bearer = HTTPBearer(auto_error=False)


async def get_current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "身份验证失败", headers={"WWW-Authenticate": "Bearer"})
    try:
        principal = await authenticated_principal(verify_access(credentials.credentials))
        request.state.principal = principal
        return principal
    except SecurityError as exc:
        raise HTTPException(exc.status_code, exc.code, headers={"WWW-Authenticate": "Bearer"}) from None


async def require_admin(user: Principal = Depends(get_current_user)) -> Principal:
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


async def require_conversation(conversation_id: int, user: Principal):
    if not await repository.get_conversation_for_user(conversation_id, user.user_id):
        raise HTTPException(404, "会话不存在")
