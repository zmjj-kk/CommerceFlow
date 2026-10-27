import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import Settings, settings
from app.core.context_budget import ContextBudgetExceeded, enforce_request_budget
from app.core.llm import get_chat_model
from app.graph import nodes


def payload(messages, **kwargs):
    return {"model": settings.chat_model, "messages": messages, **kwargs}


def cap_at(monkeypatch, input_budget):
    monkeypatch.setattr(settings, "chat_model_context_limits", {})
    monkeypatch.setattr(settings, "chat_context_limit",
                        input_budget + settings.chat_output_reserve + settings.chat_context_safety_margin)


def test_full_request_breakdown_and_output_limit():
    model = get_chat_model()
    state = {"route": "refund_flow", "summary": "用户咨询订单1001",
             "order_data": {"order_id": "1001", "status": "已签收"},
             "evidence": "[1] 自签收日起7天，拆封不能退。",
             "messages": [HumanMessage("1001可以退吗"),
                          AIMessage("", tool_calls=[{"name": "query_order", "args": {}, "id": "c"}]),
                          ToolMessage('{"status":"已签收"}', tool_call_id="c")]}
    messages = nodes._agent_messages(state)
    raw = model._get_request_payload(messages, tools=[{"type": "function", "function": {
        "name": "query_order", "parameters": {"type": "object"}}}])
    checked, report = enforce_request_budget(raw, context_indices={2})
    assert all(report.components[k] > 0 for k in (
        "fixed_prompt_and_tools", "history_summary", "recent_dialogue", "business_material", "agent_runtime"))
    assert report.input_tokens + report.output_reserve + report.safety_margin <= report.context_limit
    assert checked[settings.chat_output_token_parameter] == settings.chat_output_reserve
    assert checked["tools"] == raw["tools"]
    assert checked["messages"] == raw["messages"]


def test_schema_overflow_blocks_even_with_tiny_history(monkeypatch):
    cap_at(monkeypatch, 500)
    request = payload([{"role": "user", "content": "hi"}], tools=[{
        "type": "function", "function": {"name": "big", "description": "说明" * 2000}}])
    with pytest.raises(ContextBudgetExceeded) as caught:
        enforce_request_budget(request)
    assert caught.value.report.components["fixed_prompt_and_tools"] > 500


def test_drops_whole_noncritical_turn_and_keeps_cache_prefix(monkeypatch):
    messages = [{"role": "system", "content": "static"},
                {"role": "user", "content": "你好" * 500},
                {"role": "assistant", "content": "欢迎" * 500},
                {"role": "user", "content": "订单1001"}]
    request = payload(messages, tools=[{"type": "function", "function": {"name": "stable"}}])
    cap_at(monkeypatch, 600)
    checked, report = enforce_request_budget(request, allow_history_trim=True)
    assert checked["messages"] == [messages[0], messages[-1]]
    assert checked["tools"] == request["tools"]
    assert report.actions == ("remove_old_noncritical_turn",)
    assert len(request["messages"]) == 4  # State/input never mutated


@pytest.mark.parametrize("critical", ["订单1001", "只有未拆封才能退款", "权限校验不通过", "状态：结果待核实"])
def test_critical_old_turn_is_not_disposable(monkeypatch, critical):
    cap_at(monkeypatch, 500)
    messages = [{"role": "user", "content": critical + "材料" * 500},
                {"role": "assistant", "content": "收到"}, {"role": "user", "content": "请继续"}]
    with pytest.raises(ContextBudgetExceeded):
        enforce_request_budget(payload(messages), allow_history_trim=True)


def test_latest_oversized_message_cannot_fall_back_to_oversized_window(monkeypatch):
    cap_at(monkeypatch, 500)
    messages = nodes._agent_messages({"messages": [HumanMessage("很长的问题" * 4000)]})
    with pytest.raises(ContextBudgetExceeded):
        get_chat_model()._get_request_payload(messages)


def test_tool_json_compaction_is_lossless_and_keeps_numeric_lexemes(monkeypatch):
    content = '{\n' + ' ' * 2000 + '"order_id": "001001", "amount": 1.2300, "note": "保留 空格", "allowed": false}'
    messages = [{"role": "user", "content": "订单1001"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function",
                    "function": {"name": "query_order", "arguments": "{}"}}]},
                {"role": "tool", "content": content, "tool_call_id": "c"}]
    cap_at(monkeypatch, 900)
    checked, report = enforce_request_budget(payload(messages), allow_history_trim=True)
    compact = checked["messages"][-1]["content"]
    assert json.loads(compact) == json.loads(content)
    assert "1.2300" in compact and "保留 空格" in compact
    assert checked["messages"][-1]["tool_call_id"] == "c"
    assert "compact_tool_json_losslessly" in report.actions
    assert messages[-1]["content"] == content


def test_non_json_tool_text_is_never_sliced(monkeypatch):
    cap_at(monkeypatch, 400)
    request = payload([{"role": "tool", "tool_call_id": "c", "content": "不能退款，除非未拆封。" * 300}])
    with pytest.raises(ContextBudgetExceeded):
        enforce_request_budget(request)
    assert request["messages"][0]["content"].endswith("除非未拆封。")


def test_model_override_and_caller_output_limit(monkeypatch):
    monkeypatch.setattr(settings, "chat_model_context_limits", {"small": 3600})
    checked, report = enforce_request_budget(payload([{"role": "user", "content": "hi"}],
                                                    model="small", max_completion_tokens=99999))
    assert report.context_limit == 3600
    assert checked[settings.chat_output_token_parameter] == settings.chat_output_reserve
    checked, _ = enforce_request_budget({**checked, "max_completion_tokens": 10})
    assert checked[settings.chat_output_token_parameter] == 10


@pytest.mark.parametrize("streaming", [False, True])
def test_sync_transport_is_blocked_before_network(monkeypatch, streaming):
    cap_at(monkeypatch, 300)
    model = get_chat_model(streaming=streaming)
    with pytest.raises(ContextBudgetExceeded):
        model.invoke([HumanMessage("材料" * 2000)])


@pytest.mark.parametrize("streaming", [False, True])
async def test_async_transport_is_blocked_before_network(monkeypatch, streaming):
    cap_at(monkeypatch, 300)
    model = get_chat_model(streaming=streaming)
    with pytest.raises(ContextBudgetExceeded):
        await model.ainvoke([HumanMessage("材料" * 2000)])


async def test_agent_budget_failure_stops_and_records_reason(monkeypatch):
    cap_at(monkeypatch, 500)

    async def no_specs():
        return []

    monkeypatch.setattr(nodes.registry, "get_all_specs", no_specs)
    state = {"route": "refund_flow", "summary": "订单1001",
             "summary_upto_msg_id": 2,
             "messages": [HumanMessage("旧问题", id="db-1"), HumanMessage("能退吗", id="db-3")],
             "order_data": {"order_id": "1001", "status": "已签收"},
             "evidence": "只有未拆封且签收7天内可退。" * 1000, "steps": 2}
    out = await nodes.main_agent(state)
    assert out["steps"] == 3
    assert not out["messages"][0].tool_calls
    assert out["trace"]["context_budget"]["reason"] == "protected_context_exceeds_budget"
    assert out["suggested_actions"] == [{"type": "transfer_human"}]
    assert state["order_data"]["order_id"] == "1001"
    assert state["summary_upto_msg_id"] == 2


def test_budget_config_rejects_no_input_space():
    with pytest.raises(ValueError, match="context limits"):
        Settings(chat_context_limit=100, chat_output_reserve=100)


@pytest.mark.parametrize("parameter", ["max_tokens", "max_completion_tokens"])
def test_final_output_parameter_matches_upstream_config(monkeypatch, parameter):
    monkeypatch.setattr(settings, "chat_output_token_parameter", parameter)
    request = get_chat_model()._get_request_payload([HumanMessage("你好")], max_tokens=20)
    assert request[parameter] == 20
    assert ({"max_tokens", "max_completion_tokens"} - {parameter}).isdisjoint(request)


def test_extra_body_cannot_override_budgeted_messages_or_output():
    with pytest.raises(ContextBudgetExceeded, match="unsupported_request_override"):
        enforce_request_budget(payload([{"role": "user", "content": "hi"}],
                                       extra_body={"max_tokens": 999999}))


def test_unknown_language_business_facts_remain_protected(monkeypatch):
    cap_at(monkeypatch, 500)
    messages = [{"role": "user", "content": "Only sealed goods can be returned. " * 100},
                {"role": "assistant", "content": "Understood"}, {"role": "user", "content": "Continue"}]
    with pytest.raises(ContextBudgetExceeded):
        enforce_request_budget(payload(messages), allow_history_trim=True)


async def test_structured_schema_is_counted_and_budget_failure_not_retried(monkeypatch):
    from pydantic import BaseModel, Field
    from app.core import llm

    class Schema(BaseModel):
        result: str = Field(description="字段描述" * 1000)

    cap_at(monkeypatch, 700)
    calls = []
    original = llm._CumulativeUsageChatOpenAI._get_request_payload

    def counted(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(llm._CumulativeUsageChatOpenAI, "_get_request_payload", counted)
    with pytest.raises(ContextBudgetExceeded) as caught:
        await llm.structured(Schema).ainvoke("hi")
    assert len(calls) == 1
    assert caught.value.report.components["fixed_prompt_and_tools"] > 700


def test_duplicate_json_keys_are_not_compacted(monkeypatch):
    cap_at(monkeypatch, 500)
    content = '{"condition": "可退",' + ' ' * 1000 + '"condition": "不可退"}'
    with pytest.raises(ContextBudgetExceeded) as caught:
        enforce_request_budget(payload([{"role": "tool", "content": content, "tool_call_id": "c"}]))
    assert "compact_tool_json_losslessly" not in caught.value.report.actions
