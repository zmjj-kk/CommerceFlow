import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_current_user, require_conversation
from app.graph import runtime
from app.schemas.actions import (CreateRefundRequest, CreateRefundResponse, CreateTicketRequest,
                                CreateTicketResponse, PrepareActionRequest, ResumeRequest)
from app.security.principal import Principal
from app.services import tickets
from app.services.refunds import submit_refund_request

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/actions/prepare")
async def prepare_action(req: PrepareActionRequest, user: Principal = Depends(get_current_user)):
    return await tickets.prepare(user.user_id, req.conversation_id, req.tool_name, req.arguments)


@router.post("/api/actions/create-ticket", response_model=CreateTicketResponse)
async def create_ticket_action(req: CreateTicketRequest, user: Principal = Depends(get_current_user)):
    number = await tickets.submit(user_id=user.user_id, conversation_id=req.conversation_id,
        kind="create_ticket", args={"description": req.description, "ticket_type": req.ticket_type},
        approval_id=req.approval_id)
    return CreateTicketResponse(ticket_no=number)


@router.post("/api/actions/create-refund", response_model=CreateRefundResponse)
async def create_refund_action(req: CreateRefundRequest, user: Principal = Depends(get_current_user)):
    number = await submit_refund_request(user_id=user.user_id, conversation_id=req.conversation_id,
        order_id=req.order_id, reason=req.reason, approval_id=req.approval_id)
    return CreateRefundResponse(ticket_no=number)


def _sse(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/api/actions/resume")
async def resume_action(req: ResumeRequest, user: Principal = Depends(get_current_user)):
    await require_conversation(req.conversation_id, user)
    resume_value = req.order_id if req.order_id is not None else {
        "confirmed": req.confirmed, "approval_id": req.approval_id}
    await runtime.validate_resume(user.user_id, req.conversation_id, resume_value)

    async def event_stream():
        try:
            async for ev in runtime.stream_resume(user.user_id, req.conversation_id, resume_value):
                kind = ev["type"]
                if kind == "delta":
                    yield _sse({"delta": ev["text"]})
                elif kind == "interrupt":
                    yield _sse({"event": kind, **{k: v for k, v in ev.items() if k != "type"}})
                elif kind in {"tool", "citations", "actions", "done"}:
                    yield _sse({"event": kind, **{k: v for k, v in ev.items() if k != "type"}})
        except Exception:
            logger.error("resume_failed conv=%s", req.conversation_id)
            yield "event: error\n" + _sse({"message": "续跑暂时不可用，请稍后重试"})
            return
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/api/actions/operations/{approval_id}")
async def operation_status(approval_id: str, user: Principal = Depends(get_current_user)):
    from fastapi import HTTPException
    import app.db.base as db
    from app.db.models import Approval, BusinessOperation
    async with db.async_session() as s:
        grant = await s.get(Approval, approval_id)
        if not grant or grant.user_id != user.user_id:
            raise HTTPException(404, "操作不存在")
        await require_conversation(grant.conversation_id, user)
        operation = await s.get(BusinessOperation, grant.operation_id)
        if operation:
            return {"state": "completed", "ticket_no": operation.ticket_no, "kind": operation.kind}
        # A timed-out in-flight write may still finish; absence is not permission to reissue.
        return {"state": "pending_or_not_executed", "expires_at": grant.expires_at.isoformat() + "Z"}
