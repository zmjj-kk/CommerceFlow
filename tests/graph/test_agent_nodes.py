# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.graph import nodes
from tests.security.conftest import encoded_password, secure_db


def test_agent_messages_injects_evidence_on_knowledge():
    msgs = nodes._agent_messages(
        {"route": "knowledge", "evidence": "[1] 退货: 7天",
         "messages": [HumanMessage("能退吗")]})
    # 证据进的是用户侧消息,不进 system——system 要保持逐字不变才能命中前缀缓存
    assert isinstance(msgs[0], SystemMessage)
    assert "[1] 退货: 7天" not in msgs[0].content
    ctx = [m for m in msgs if isinstance(m, HumanMessage) and "[1] 退货: 7天" in m.content]
    assert len(ctx) == 1
    assert "query_faq" in ctx[0].content            # 指示别再检索,跟着证据一起搬过来了
    assert msgs[-2].content == "能退吗" and msgs[-1] is ctx[0]   # 紧跟用户那句之后


def test_agent_messages_no_evidence_on_business():
    msgs = nodes._agent_messages({"route": "business", "messages": [HumanMessage("订单1001")]})
    assert isinstance(msgs[0], SystemMessage)
    assert all("已检索到的知识证据" not in (m.content or "") for m in msgs)  # 整个列表都不该有


@pytest.fixture()
def builtin_only_specs(monkeypatch):
    """ch08:节点每轮现拉内置+MCP;单测锚定为仅内置,免网络依赖。"""
    from app.tools import registry

    async def only_builtin():
        return registry.builtin_specs()

    monkeypatch.setattr(nodes.registry, "get_all_specs", only_builtin)


@pytest.mark.asyncio
async def test_main_agent_accumulates_steps_and_tokens(monkeypatch, builtin_only_specs):
    ai = AIMessage("好的", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    class FakeModel:
        def bind_tools(self, tools):
            return self
        def bind(self, **kwargs):
            return self
        async def ainvoke(self, msgs, config=None):
            return ai

    monkeypatch.setattr(nodes, "get_chat_model", lambda **k: FakeModel())
    out = await nodes.main_agent({"messages": [HumanMessage("hi")], "steps": 1, "tokens_used": 100})
    assert out["steps"] == 2
    assert out["tokens_used"] == 115
    assert out["messages"][0] is ai


@pytest.mark.asyncio
async def test_agent_tools_executes_normal_tool(monkeypatch, builtin_only_specs):
    from langchain_core.messages import ToolMessage

    from app.tools.engine import ToolRun

    async def fake_exec(tc, cid, specs, **kw):
        return ToolRun(tool_call_id=tc["id"], name=tc["name"], ok=True, status="成功",
                       tool_message=ToolMessage(content='{"status":"已发货"}',
                                                tool_call_id=tc["id"], name=tc["name"]))

    monkeypatch.setattr(nodes.engine, "execute_tool_call", fake_exec)
    ai = AIMessage("", tool_calls=[{"name": "query_logistics", "args": {"order_id": "1001"}, "id": "t1"}])
    out = await nodes.agent_tools({"messages": [ai], "conversation_id": 5})
    assert out["messages"][0].name == "query_logistics"
    assert not out.get("suggested_actions")


@pytest.mark.asyncio
async def test_agent_tools_create_ticket_missing_args_no_interrupt(monkeypatch, builtin_only_specs):
    """ch08:create_ticket 缺必填 → 不弹确认卡,交引擎按「校验拦下」回灌(模型据此追问用户)。
    确认流两分支(interrupt→resume)在 tests/graph/test_ch08_confirm_ticket.py 做 graph 级覆盖。"""
    from langchain_core.messages import ToolMessage

    from app.tools.engine import ToolRun

    seen = {}

    async def fake_exec(tc, cid, specs, **kw):
        seen["confirmed"] = kw.get("confirmed", False)
        return ToolRun(tool_call_id=tc["id"], name=tc["name"], ok=False, status="校验拦下",
                       tool_message=ToolMessage(content="参数校验未通过:'description' is a required property",
                                                tool_call_id=tc["id"], name=tc["name"], status="error"))

    monkeypatch.setattr(nodes.engine, "execute_tool_call", fake_exec)
    ai = AIMessage("", tool_calls=[{"name": "create_ticket",
                    "args": {"ticket_type": "售后"}, "id": "t9"}])   # 缺 description
    out = await nodes.agent_tools({"messages": [ai], "conversation_id": 5})
    assert not out.get("suggested_actions")            # 旧「拦成按钮」路径已删
    assert seen["confirmed"] is False                  # 未确认,引擎侧权限门兜底
    assert "参数校验未通过" in out["messages"][0].content


@pytest.mark.asyncio
async def test_agent_tools_refund_goes_through_gate(secure_db, builtin_only_specs):
    ai = AIMessage("", tool_calls=[{"name": "submit_refund", "args": {"order_id": "1001"}, "id": "t8"}])
    out = await nodes.agent_tools({"messages": [ai], "conversation_id": 1,
                                   "user_id": "alice", "run_id": "run1"})
    assert out["suggested_actions"][0]["type"] == "refund_form"
    assert out["messages"][0].status == "success"


async def test_foreign_order_has_no_refund_form(secure_db, builtin_only_specs):
    ai = AIMessage("", tool_calls=[{"name": "submit_refund", "args": {"order_id": "2002"}, "id": "t9"}])
    out = await nodes.agent_tools({"messages": [ai], "conversation_id": 1,
                                   "user_id": "alice", "run_id": "run1"})
    assert "refund_form" not in [a["type"] for a in out["suggested_actions"]]
    assert out["messages"][0].status == "error"
