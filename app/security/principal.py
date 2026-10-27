from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str = "user"
    tenant_id: str | None = None
    session_id: str = ""


CurrentUser = Principal
