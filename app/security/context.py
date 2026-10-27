from dataclasses import dataclass, field
from uuid import uuid4
from app.security.principal import Principal

POLICY_REVISION = "2026-10-21-v1"


@dataclass(frozen=True)
class SecurityContext:
    principal: Principal
    conversation_id: int
    run_id: str
    tool_call_id: str
    operation_id: str = field(default_factory=lambda: uuid4().hex)
    mode: str = "default"
    policy_revision: str = POLICY_REVISION
    deadline: float | None = None
