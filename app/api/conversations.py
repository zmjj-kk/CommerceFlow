# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""ch07 验收配套:会话列表 + 历史消息回载(只读,前端多会话切换用)。"""
from fastapi import APIRouter, Depends, HTTPException
from app.api.deps import get_current_user
from app.security.principal import Principal

from app.db import repository

router = APIRouter()


@router.get("/api/conversations")
async def list_conversations(user: Principal = Depends(get_current_user)):
    items = await repository.list_conversations(user.user_id)
    return {"items": items}


@router.get("/api/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: int, user: Principal = Depends(get_current_user)):
    if await repository.get_conversation_for_user(conversation_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = await repository.list_dialog_messages(conversation_id)
    return {"items": [
        {"role": m.role, "content": m.content or "",
         "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in msgs
    ]}
