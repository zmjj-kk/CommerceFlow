import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

import app.db.base as db
from app.db.models import Approval, SecurityDecision
from app.security.context import POLICY_REVISION, SecurityContext
from app.security.errors import SecurityError
from app.security.tokens import now_utc
from app.security.transactions import transaction


def argument_hash(args: dict) -> str:
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def tool_identity(spec) -> str:
    from app.tools.mcp_client import schema_fingerprint
    endpoint = getattr(spec, "endpoint", "") or ""
    return f"{spec.source}:{spec.mcp_server or '-'}:{spec.name}:{schema_fingerprint(spec.json_schema)}:{hashlib.sha256(endpoint.encode()).hexdigest()}"


def audit_row(context: SecurityContext, tool_name: str, identity: str, args: dict, decision: str, reason: str):
    return SecurityDecision(id=uuid4().hex, user_id=context.principal.user_id,
        conversation_id=context.conversation_id, run_id=context.run_id[:64],
        tool_call_id=context.tool_call_id[:64], operation_id=context.operation_id,
        tool_name=tool_name[:128], tool_identity=identity[:512], argument_hash=argument_hash(args),
        policy_revision=POLICY_REVISION, decision=decision, reason_code=reason)


async def audit_decision(context, spec, args, decision):
    async with transaction() as s:
        s.add(audit_row(context, spec.name, tool_identity(spec), args, decision.action, decision.reason_code))


def validate_row(row: Approval | None, context: SecurityContext, name: str, identity: str, args: dict):
    if (not row or row.user_id != context.principal.user_id or
            row.conversation_id != context.conversation_id or row.run_id != context.run_id or
            row.tool_call_id != context.tool_call_id or row.tool_name != name or
            row.tool_identity != identity or row.argument_hash != argument_hash(args) or
            row.policy_revision != POLICY_REVISION or row.expires_at <= now_utc()):
        raise SecurityError("approval_invalid", 403)


async def issue(context: SecurityContext, name: str, identity: str, args: dict) -> Approval:
    from app.config import settings
    # Stable across LangGraph interrupt replays; args/identity/policy changes produce a new grant.
    key = hashlib.sha256(f"{context.principal.user_id}:{context.conversation_id}:{context.run_id}:{context.tool_call_id}:{name}:{identity}:{argument_hash(args)}:{POLICY_REVISION}".encode()).hexdigest()[:32]
    async with transaction() as s:
        row = await s.get(Approval, key)
        if row:
            validate_row(row, context, name, identity, args)
            return row
        row = Approval(approval_id=key, operation_id=context.operation_id, user_id=context.principal.user_id,
            conversation_id=context.conversation_id, run_id=context.run_id,
            tool_call_id=context.tool_call_id, tool_name=name, tool_identity=identity,
            argument_hash=argument_hash(args), policy_revision=POLICY_REVISION,
            expires_at=now_utc() + timedelta(seconds=settings.approval_ttl_seconds))
        s.add(row)
        s.add(audit_row(context, name, identity, args, "ask", "approval_issued"))
    return row


async def check(approval_id: str, context: SecurityContext, name: str, identity: str, args: dict):
    async with db.async_session() as s:
        row = await s.get(Approval, approval_id)
        validate_row(row, context, name, identity, args)
        if row.consumed_at:
            raise SecurityError("approval_already_used", 409)
        return row


async def consume(s, approval_id, context, name, identity, args):
    row = (await s.execute(select(Approval).where(
        Approval.approval_id == approval_id).with_for_update())).scalar_one_or_none()
    validate_row(row, context, name, identity, args)
    changed = await s.execute(update(Approval).where(
        Approval.approval_id == approval_id, Approval.consumed_at.is_(None)
    ).values(consumed_at=now_utc()))
    if changed.rowcount != 1:
        raise SecurityError("approval_already_used", 409)
    return row
