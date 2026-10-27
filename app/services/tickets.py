from uuid import uuid4
from dataclasses import replace
import logging
logger = logging.getLogger(__name__)

from sqlalchemy import select

import app.db.base as db
from app.db.models import Approval, BusinessOperation, Conversation, Order, RefundRequest, Ticket
from app.security import approvals
from app.security.context import SecurityContext
from app.security.errors import SecurityError
from app.security.principal import Principal
from app.security.tokens import now_utc
from app.security.transactions import transaction

REFUND_REASONS = {"七天无理由", "质量问题", "发错货", "不想要了", "其他"}
TICKET_TYPES = {"售后", "投诉", "咨询"}


def validate_arguments(kind: str, args: dict):
    expected = {"description", "ticket_type"} if kind == "create_ticket" else {"order_id", "reason"}
    if not isinstance(args, dict) or set(args) != expected or any(
            not isinstance(v, str) or not v.strip() for v in args.values()):
        raise SecurityError("invalid_arguments", 422)
    if kind == "create_ticket":
        if args["ticket_type"] not in TICKET_TYPES or len(args["description"]) > 4000:
            raise SecurityError("invalid_arguments", 422)
    elif kind == "create_refund":
        if args["reason"] not in REFUND_REASONS or len(args["order_id"]) > 64:
            raise SecurityError("invalid_arguments", 422)
    else:
        raise SecurityError("unknown_operation", 403)


async def resources(s, user_id: str, cid: int, kind: str, args: dict, *, lock: bool = False):
    q = select(Conversation).where(Conversation.id == cid, Conversation.user_id == user_id)
    conv = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if not conv:
        logger.warning("resource_denied principal=%s action=%s conversation=%s", user_id, kind, cid)
        raise SecurityError("resource_not_found", 404)
    order = None
    if kind == "create_refund":
        q = select(Order).where(Order.order_id == args["order_id"], Order.user_id == user_id)
        order = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
        if not order:
            logger.warning("resource_denied principal=%s action=%s conversation=%s", user_id, kind, cid)
            raise SecurityError("resource_not_found", 404)
        if (not order.refundable or order.status not in {"已付款", "已发货", "已签收"} or
                not order.refund_deadline or order.refund_deadline <= now_utc()):
            raise SecurityError("order_not_refundable", 409)
    return conv, order


def identity(kind: str) -> str:
    if kind == "create_ticket":
        from app.tools.registry import get_builtin_spec
        return approvals.tool_identity(get_builtin_spec(kind))
    return "http:create_refund:v1"


async def prepare(user_id: str, cid: int, kind: str, args: dict):
    from app.config import settings
    if settings.tool_security_mode == 'plan':
        raise SecurityError('plan_mode_read_only', 403)
    validate_arguments(kind, args)
    async with db.async_session() as s:
        await resources(s, user_id, cid, kind, args)
    context = SecurityContext(Principal(user_id), cid, uuid4().hex, uuid4().hex)
    grant = await approvals.issue(context, kind, identity(kind), args)
    return {"approval_id": grant.approval_id, "preview": args, "expires_at": grant.expires_at.isoformat() + "Z"}


async def submit(*, user_id: str, conversation_id: int, kind: str, args: dict,
                 approval_id: str, context: SecurityContext | None = None,
                 expected_identity: str | None = None) -> str:
    from app.config import settings
    if settings.tool_security_mode == 'plan':
        raise SecurityError('plan_mode_read_only', 403)
    validate_arguments(kind, args)
    async with transaction() as s:
        # Serialize related business effects, then consume the exact grant in this transaction.
        conv, order = await resources(s, user_id, conversation_id, kind, args, lock=True)
        row = (await s.execute(select(Approval).where(Approval.approval_id == approval_id)
                             .with_for_update())).scalar_one_or_none()
        if not row:
            raise SecurityError("approval_invalid", 403)
        context = context or SecurityContext(Principal(user_id), conversation_id, row.run_id,
                                             row.tool_call_id, operation_id=row.operation_id)
        if context.principal.user_id != user_id or context.conversation_id != conversation_id:
            raise SecurityError("approval_invalid", 403)
        context = replace(context, operation_id=row.operation_id)
        ident = expected_identity or identity(kind)
        approvals.validate_row(row, context, kind, ident, args)
        prior = await s.get(BusinessOperation, row.operation_id)
        if prior:
            if (prior.user_id != user_id or prior.conversation_id != conversation_id or
                    prior.kind != kind or prior.argument_hash != approvals.argument_hash(args)):
                raise SecurityError("operation_conflict", 409)
            return prior.ticket_no
        if order:
            existing = (await s.execute(select(RefundRequest).where(
                RefundRequest.order_id == order.order_id))).scalar_one_or_none()
            if existing:
                raise SecurityError("refund_already_requested", 409)
        await approvals.consume(s, approval_id, context, kind, ident, args)
        ticket_no = "T" + uuid4().hex[:30]
        description = (args["description"] if kind == "create_ticket" else
                       f"退款申请 订单号={args['order_id']} 原因={args['reason']}")
        ticket_type = args["ticket_type"] if kind == "create_ticket" else "退款"
        s.add(Ticket(ticket_no=ticket_no, conversation_id=conversation_id,
                     description=description, ticket_type=ticket_type))
        conv.status = "已转人工"
        await s.flush()
        s.add(BusinessOperation(operation_id=row.operation_id, user_id=user_id,
            conversation_id=conversation_id, kind=kind,
            argument_hash=approvals.argument_hash(args), ticket_no=ticket_no))
        await s.flush()
        if order:
            s.add(RefundRequest(id=uuid4().hex, order_id=order.order_id, user_id=user_id,
                conversation_id=conversation_id, operation_id=row.operation_id, reason=args["reason"]))
        s.add(approvals.audit_row(context, kind, ident, args, "allow", "business_committed"))
    return ticket_no
