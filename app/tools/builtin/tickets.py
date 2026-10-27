import asyncio
from typing import Annotated, Literal

from langchain_core.tools import InjectedToolArg, tool

from app.config import settings
from app.security.context import SecurityContext
from app.security.principal import Principal
from app.services import tickets
from app.tools import registry


@tool
async def create_ticket(
    description: str,
    ticket_type: Literal["售后", "投诉", "咨询"],
    conversation_id: Annotated[int, InjectedToolArg],
    user_id: Annotated[str, InjectedToolArg],
    approval_id: Annotated[str, InjectedToolArg],
    run_id: Annotated[str, InjectedToolArg],
    tool_call_id: Annotated[str, InjectedToolArg],
    tool_identity: Annotated[str, InjectedToolArg],
) -> dict:
    """创建人工工单，描述和类别需用户明确提供；后端会显示精确预览并请求确认。"""
    if settings.demo_ticket_delay_seconds > 0:
        await asyncio.sleep(settings.demo_ticket_delay_seconds)
    context = SecurityContext(Principal(user_id), conversation_id, run_id, tool_call_id)
    ticket_no = await tickets.submit(user_id=user_id, conversation_id=conversation_id,
        kind="create_ticket", args={"description": description, "ticket_type": ticket_type},
        approval_id=approval_id, context=context, expected_identity=tool_identity)
    return {"ticket_no": ticket_no, "status": "已转人工"}


registry.register(registry.spec_from_langchain_tool(
    create_ticket, source="builtin", inject_conversation=True, inject_user_id=True))
