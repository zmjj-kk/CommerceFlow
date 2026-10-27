from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepareActionRequest(StrictRequest):
    conversation_id: int = Field(gt=0)
    tool_name: Literal["create_ticket", "create_refund"]
    arguments: dict


class CreateTicketRequest(StrictRequest):
    conversation_id: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=4000)
    ticket_type: Literal["售后", "投诉", "咨询"]
    approval_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CreateTicketResponse(BaseModel):
    ticket_no: str
    status: str = "已转人工"


class CreateRefundRequest(StrictRequest):
    conversation_id: int = Field(gt=0)
    order_id: str = Field(min_length=1, max_length=64)
    reason: Literal["七天无理由", "质量问题", "发错货", "不想要了", "其他"]
    approval_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CreateRefundResponse(BaseModel):
    ticket_no: str
    status: str = "退款申请已提交"


class ResumeRequest(StrictRequest):
    conversation_id: int = Field(gt=0)
    order_id: str | None = Field(default=None, min_length=1, max_length=64)
    confirmed: bool | None = None
    approval_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def valid_resume(self):
        if (self.order_id is None) == (self.confirmed is None):
            raise ValueError("只允许传订单选择或审批确认之一")
        if self.confirmed is True and not self.approval_id:
            raise ValueError("确认需要 approval_id")
        if self.order_id is not None and self.approval_id:
            raise ValueError("订单选择不能附带审批")
        return self
