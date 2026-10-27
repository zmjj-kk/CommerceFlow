from dataclasses import dataclass

from app.config import settings
from app.db import repository
from app.security.context import POLICY_REVISION, SecurityContext
from app.security.policy import policy_for

@dataclass(frozen=True)
class Decision:
    action: str
    reason_code: str
    policy_revision: str = POLICY_REVISION


async def decide(context: SecurityContext, spec, args: dict) -> Decision:
    policy = policy_for(spec)
    if policy is None:
        return Decision("deny", "untrusted_tool_identity")
    if spec.source == "builtin":
        from app.tools.registry import get_builtin_spec
        registered = get_builtin_spec(spec.name)
        if not registered or registered.tool is not spec.tool or registered.json_schema != spec.json_schema:
            return Decision("deny", "unregistered_builtin")
    if context.deadline is not None:
        import time
        if time.monotonic() >= context.deadline:
            return Decision("deny", "deadline_exceeded")
    if context.policy_revision != POLICY_REVISION:
        return Decision("deny", "stale_policy")
    if context.mode not in {"plan", "default", "auto", "bypass"}:
        return Decision("deny", "invalid_mode")
    if context.mode == "bypass" and not settings.tool_allow_bypass:
        return Decision("deny", "bypass_disabled")
    uid = context.principal.user_id
    if not uid or not await repository.get_active_user(uid):
        return Decision("deny", "principal_inactive")
    if not await repository.get_conversation_for_user(context.conversation_id, uid):
        return Decision("deny", "conversation_not_owned")
    if spec.source == "mcp":
        from app.tools.mcp_client import trusted_spec
        if not trusted_spec(spec):
            return Decision("deny", "mcp_identity_changed")
    if policy.resource == "order" and not await repository.get_order_for_user(str(args.get("order_id") or ""), uid):
        return Decision("deny", "resource_not_found")
    if policy.resource == "tracking" and not await repository.get_order_by_tracking_for_user(
            str(args.get("tracking_no") or ""), uid):
        return Decision("deny", "resource_not_found")
    if policy.approval:
        if context.mode == "plan":
            return Decision("deny", "plan_mode_read_only")
        return Decision("ask", "approval_required")
    return Decision("allow", "explicit_allow")
