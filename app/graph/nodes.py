# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from app.config import settings
from app.core import coref
from app.core import intent as intent_mod
from app.core import memory
from app.core import query_understanding, retrieval, selfcheck
from app.core.confidence import compute_evidence_confidence, snapshot_from_hits
from app.core.llm import get_chat_model
from app.core.context_budget import ContextBudgetExceeded
from app.core.observability import tag_intent
from app.core.prompts import (
    AGENT_SYSTEM, COMPLAINT_REPLY_TEXT, FALLBACK_REPLY_TEXT,
    REFUND_JUDGE_HINT, SCRIPT_REPLY_CHITCHAT, SCRIPT_REPLY_OTHER,
)
from app.db import repository
from app.graph.routing import INTENT_TO_ROUTE
from app.tools import business, engine, registry
from app.kb.domain import citation

logger = logging.getLogger(__name__)

COMPLAINT_REPLY = COMPLAINT_REPLY_TEXT
FALLBACK_REPLY = FALLBACK_REPLY_TEXT


def _user_text(state) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.content or ""
    return ""


def _history_text(state, max_turns: int = 6) -> str:
    """摘要行 + 滑窗内最近若干轮(不含本轮最后一条 human),供 coref/意图读上下文。
    ch07:先按摘要边界切窗再取尾——跨滑窗的指代(「最开始那个订单」)靠摘要行兜住。"""
    msgs = memory.build_window(state.get("messages", []),
                               state.get("summary_upto_msg_id") or 0,
                               settings.context_window_max_tokens)
    prior = msgs[:-1] if msgs else []
    lines = []
    for m in prior[-max_turns:]:
        role = "用户" if isinstance(m, HumanMessage) else "客服"
        text = m.content if isinstance(m.content, str) else ""
        if text:
            lines.append(f"{role}:{text}")
    body = "\n".join(lines)
    head = memory.summary_line(state.get("summary"))
    return f"{head}\n{body}".strip() if head else body


# 4+ 位连续数字视作订单号;用 lookaround 而非 \b——CJK 与数字同属 \w,\b 在「订单1001」处不成立
_ORDER_RE = re.compile(r"(?<!\d)(\d{4,})(?!\d)")


def _extract_order_id(text: str) -> str | None:
    m = _ORDER_RE.search(text or "")
    return m.group(1) if m else None


async def fetch_order(state) -> dict:
    """退款子流程第一步:抽订单号;缺了、或者报的不是自己的单,都 interrupt 弹订单选择器
    等前端点选(resume 回填);拿到后 order_snapshot 取订单数据。
    interrupt 之前只做只读(resume 时本节点从头重跑)。

    归属校验放在这里而不是只放在工具里:退款子流程是确定性节点,不经 agent_tools,
    工具那道校验够不着。用户随口报的号、resume 端点回传的号,都得过同一道判断。"""
    uid = state.get("user_id", "")
    oid = state.get("order_id") or _extract_order_id(
        state.get("resolved_query") or _user_text(state))
    while not await business.owns_order(uid, str(oid or "")):
        orders = await business.list_user_orders(uid)                        # 只读,可安全重跑
        oid = interrupt({"type": "select_order", "orders": orders})    # resume 回填订单号
    data = await business.order_snapshot(oid, uid)
    return {"order_id": oid, "order_data": data,
            "trace": {"fetch_order": {"order_id": oid}}}


async def retrieve_policy(state) -> dict:
    """退款子流程强制检索政策:Query 扩写 3 条 → 多 query 各检索一次 → 按 chunk id 去重合并
    (保留每 id 最高分)→ 按分降序拼编号证据。产出注入 main_agent 作「能不能退」的判据(README L194:
    不让模型凭记忆答)。库里知识一份,扩写只在检索侧现查现用。"""
    base = state.get("resolved_query") or _user_text(state)
    od = state.get("order_data") or {}
    seed = f"{base} {od.get('status', '')}".strip()
    queries = await query_understanding.expand_queries(seed)

    merged: dict = {}                       # chunk id -> 最高分 hit
    for q in queries:
        for h in await retrieval.search_knowledge(q, strategy="hybrid_rerank", bm25_text=q):
            cur = merged.get(h["id"])
            if cur is None or h["rerank_score"] > cur["rerank_score"]:
                merged[h["id"]] = h
    ranked = sorted(merged.values(), key=lambda h: h["rerank_score"], reverse=True)
    arranged = retrieval.arrange_head_tail(ranked)
    citations = [
        citation(h, i + 1)
        for i, h in enumerate(arranged)
    ]
    evidence = "\n".join(f"[{c['n']}] {c['question']}: {c['answer']}" for c in citations)
    return {"evidence": evidence, "citations": citations,
            "trace": {"retrieve_policy": {"queries": queries, "hits": len(ranked)}}}


async def script_reply(state) -> dict:
    """fallback_script 出口:闲聊/其他 兜底话术(零模型),按 intent 分文案——闲聊把话题引回产品,
    其他请用户说具体些。分流后立即命中、不进 Agent。
    (与 ch05 知识路证据弱的 fallback_reply 是两码事,名字相近但不同节点、不同触发点,勿混。)"""
    text = SCRIPT_REPLY_OTHER if state.get("intent") == "其他" else SCRIPT_REPLY_CHITCHAT
    return {"answer": text, "trace": {"route": "fallback_script"}}


async def complaint_reply(state) -> dict:
    """投诉:安抚话术 + 转人工/建工单两个可选项(后端不执行,交前端自选)。"""
    actions = [
        {"type": "transfer_human"},
        {"type": "create_ticket",
         "draft": {"description": _user_text(state), "ticket_type": "投诉"}},
    ]
    return {"answer": COMPLAINT_REPLY, "suggested_actions": actions,
            "trace": {"route": "complaint"}}


async def fallback_reply(state) -> dict:
    """置信度兜底:证据弱回兜底话术,并把问题落低置信池留给数据飞轮
    (ch09:source 打对标签 retrieval_low_conf/self_check,随条存召回快照给审核页)。
    话术让用户「联系人工客服」,转人工按钮就得一并递给前端(actions 帧),不能光嘴上说。"""
    source = state.get("fallback_source") or "retrieval_low_conf"
    signals = state.get("trace", {}).get("confidence_signals") or {}
    reason = (f"evidence_confidence={state.get('evidence_confidence', 0.0):.3f} "
              f"signals={json.dumps(signals, ensure_ascii=False)}")
    if source == "self_check":
        reason += f" self_check={state.get('trace', {}).get('self_check', '')}"
    # 快照语义三态:走了检索且有召回=列表;走了检索但零命中=[](审核页显示「已检索,零命中」,
    # 这是"真缺知识"的强信号);没走检索(非知识路兜底,fallback_source 为空)=NULL。
    walked_retrieval = bool(state.get("fallback_source"))
    snapshot = state.get("retrieved_snapshot") or ([] if walked_retrieval else None)
    await repository.insert_low_confidence(
        state.get("conversation_id"), _user_text(state), source, reason,
        retrieved_chunks=snapshot,
    )
    return {"answer": FALLBACK_REPLY,
            "suggested_actions": [{"type": "transfer_human"}],
            "trace": {"route": "fallback"}}


async def resolve_reference(state) -> dict:
    """指代消解 + Query 改写(合一):吃最近几轮历史把半截话补成完整问句;已完整则透传。
    写 resolved_query,供 classify_intent 与 refund_flow 检索共用(意图识别不再改写)。"""
    query = _user_text(state)
    history = _history_text(state)
    # ch07 验收可观测:每轮必经此节点,摘要行+滑窗在这一条日志里直接可见
    # (model_ctx 只在进 main_agent 的路由才有,闲聊/兜底路由靠这条看)
    logger.info("history_ctx conv=%s(摘要行+滑窗,coref/意图共用)\n%s",
                state.get("conversation_id"), history or "(无历史)")
    resolved = await coref.resolve(query, history)
    mode = "rewrite" if resolved != query else "passthrough"
    return {"resolved_query": resolved, "trace": {"coref": mode}}


async def classify_intent(state) -> dict:
    """意图四件套:八类 + confidence + 「其他」兜底。吃 resolved_query(空则回落原话)+ 最近历史
    判当前意图(应对物流→退款→物流漂移)。把归属出口 route 落进 State(供 _agent_messages 判注入、
    log 留痕;route_by_intent 条件边按同一 INTENT_TO_ROUTE 分流,单一来源不漂移)。"""
    query = state.get("resolved_query") or _user_text(state)
    r = await intent_mod.classify(query, _history_text(state))
    intent, conf = r["intent"], r["confidence"]
    route = INTENT_TO_ROUTE.get(intent, "business")
    tag_intent(intent, conf)   # ch09:意图进当前 trace 的 metadata+tag,Cost Control 按意图分堆
    return {"intent": intent, "intent_confidence": conf, "route": route,
            "trace": {"intent": intent, "intent_confidence": conf, "route": route}}


async def retrieve_knowledge(state) -> dict:
    """知识类强制检索(复用 ch03/04 检索器):产出编号证据 + 证据强弱信号。
    ch09 把 ch05 最简机械闸升级成正式置信度闸(四信号加权,阈值评估集校准),
    闸位不动:检索后、进 Agent 前。快照规则:只要走了检索就存 Top3 快照——
    闸不过随落池写库;闸都过留在 state 供事后 👎 回捞(踩的往往是「检到了但答砸」)。"""
    query_raw = _user_text(state)
    u = await query_understanding.understand(query_raw)
    query = u["standard"]
    bm25_text = query + (" " + " ".join(u["expanded"]) if u["expanded"] else "")

    hits = await retrieval.search_knowledge(
        query, strategy="hybrid_rerank", bm25_text=bm25_text)
    conf = compute_evidence_confidence(hits)
    base = {"evidence_confidence": conf.score,
            "retrieved_snapshot": snapshot_from_hits(hits)}

    # 置信度闸(原机械闸升级):四信号加权总分低于校准阈值 → 证据弱,拒答落池
    if conf.score < settings.evidence_confidence_threshold:
        return {**base, "evidence_strong": False, "fallback_source": "retrieval_low_conf",
                "trace": {"forced_rag": True, "evidence_confidence": conf.score,
                          "confidence_signals": conf.signals}}

    # 语义闸:生成前自评证据够不够(README 的 useful 判断;ch09 修正:这才是 self_check 入口)
    ev_texts = [f"{h['question']} {h['answer']}" for h in hits]
    chk = await selfcheck.check_sufficient(query, ev_texts)
    if not chk["useful"]:
        return {**base, "evidence_strong": False, "fallback_source": "self_check",
                "trace": {"forced_rag": True, "evidence_confidence": conf.score,
                          "confidence_signals": conf.signals, "self_check": chk["reason"]}}

    arranged = retrieval.arrange_head_tail(hits)
    citations = [
        citation(h, i + 1)
        for i, h in enumerate(arranged)
    ]
    evidence = "\n".join(f"[{c['n']}] {c['question']}: {c['answer']}" for c in citations)
    return {**base, "evidence_strong": True, "evidence": evidence, "citations": citations,
            "trace": {"forced_rag": True, "evidence_confidence": conf.score,
                      "confidence_signals": conf.signals}}


async def confidence_check(state) -> dict:
    """置信度兜底(生成前证据闸)的实体节点:留痕门控决策供可观测;
    强/弱的实际分流在其后的条件边 confidence_gate(读 evidence_strong)。"""
    decision = "strong" if state.get("evidence_strong") else "weak"
    return {"trace": {"confidence": decision}}


_KNOWLEDGE_EVIDENCE_HINT = (
    "\n\n## 已检索到的知识证据(请据此作答,每个关键结论后标注来源编号如[1];"
    "证据已给,不要再调用 query_faq;仍可按需调用订单/物流等工具)\n"
    "型号编号逐字复制证据里的写法,证据里没有的型号不要写;带条件的结论要连条件一起说。\n"
)


def _turn_context(state) -> str:
    """本轮才有的材料:检索证据 + 退款路的订单数据。没有就返回空串。

    顺序不能反:REFUND_JUDGE_HINT 的措辞是「下面给出该订单数据与检索到的退换货政策证据」,
    它假设证据已在前文给过。"""
    parts = []
    ss = memory.summary_system(state.get("summary"))
    if ss is not None:
        parts.append("\n\n" + ss.content)     # 摘要排最前:它讲的是更早发生的事
    if state.get("evidence"):
        parts.append(_KNOWLEDGE_EVIDENCE_HINT + state["evidence"])
    if state.get("route") == "refund_flow":
        parts.append(REFUND_JUDGE_HINT + json.dumps(state.get("order_data", {}), ensure_ascii=False))
    return "".join(parts)     # 三段文本逐字沿用原来的常量,一个字都不改:这次只挪位置,
                              # 措辞一起动的话出了问题分不清是缓存改动还是提示词改动


TURN_CTX_ID = "turn-ctx"   # 哨兵:日志里据此把注入块跟真实用户消息分开。只能用 id 不能用 name
                           # ——name 会被序列化发给上游,改变请求字节;id 不会。


def _with_turn_context(window: list, turn_ctx: str) -> list:
    """把本轮材料插在滑窗里最后一条用户消息之后。

    插在这个位置而不是整个列表末尾,是为了 ReAct 轮内也能命中前缀缓存:第 2 步的
    [Sys, 用户问, 材料, AI, Tool] 完整包含第 1 步的 [Sys, 用户问, 材料] 作前缀;
    追加到末尾则第 2 步变成 [Sys, 用户问, AI, Tool, 材料],公共前缀只到用户问那条。
    窗口里没有用户消息(极端裁剪回退分支)时退化成追加到末尾,不抛异常。"""
    msg = HumanMessage(turn_ctx, id=TURN_CTX_ID)
    for i in range(len(window) - 1, -1, -1):
        if isinstance(window[i], HumanMessage):
            return [*window[:i + 1], msg, *window[i + 1:]]
    return [*window, msg]


def _agent_messages(state) -> list:
    """拼装顺序固定(ch07):人设+红线 system → 滑窗原文 → 摘要与本轮材料(紧跟用户那句)。

    整条消息列表里**只有一条 SystemMessage**,内容恒为 AGENT_SYSTEM。这不是洁癖:
    上游的 chat template 会把列表里所有 system 消息上提、合并成一个头部块渲染,所以
    「摘要单独放第二条 system」等于把它拼在了 AGENT_SYSTEM 后面,工具 schema 被挤到
    可变内容之后 —— 而 prompt caching 按渲染后的前缀精确匹配,于是整段前缀全 miss。
    实测:无摘要的会话 cache_read=2048,摘要一进 system 就掉到 0。
    摘要因此跟证据一起走用户侧那条消息。

    滑窗=摘要边界锚点切 + trim_messages token 兜底;State 全量历史不动,这里只现拼精简版。"""
    window = memory.build_window(state.get("messages", []),
                                 state.get("summary_upto_msg_id") or 0,
                                 settings.context_window_max_tokens)
    turn_ctx = _turn_context(state)
    if turn_ctx:
        window = _with_turn_context(window, turn_ctx)
    return [SystemMessage(AGENT_SYSTEM), *window]


def _log_model_context(state, msgs) -> None:
    """ch07 验收可观测:把实际发给模型的摘要与滑窗打进日志(log/app.log)。
    摘要本就几十到两百字,打全文;滑窗每条打角色+前 40 字。"""
    from langchain_core.messages.utils import count_tokens_approximately
    # 本轮材料块虽然也是 HumanMessage,但它不是用户说的话,从滑窗里摘出来单独报,
    # 否则「window=N条」会凭空多一条,验收时读不准
    ctx_block = next((m for m in msgs if getattr(m, "id", None) == TURN_CTX_ID), None)
    window = [m for m in msgs
              if not isinstance(m, SystemMessage) and getattr(m, "id", None) != TURN_CTX_ID]
    lines = []
    for m in window:
        text = m.content if isinstance(m.content, str) else ""
        lines.append(f"  [{m.type}] {text[:40]!r}")
    logger.info(
        "model_ctx conv=%s step=%s summary=%r window=%s条 本轮材料=%s tokens≈%s\n%s",
        state.get("conversation_id"), state.get("steps", 0),
        state.get("summary") or "(无)", len(window),
        f"{len(ctx_block.content)}字" if ctx_block else "无",
        count_tokens_approximately(msgs), "\n".join(lines),
    )


def execution_guard(state) -> str | None:
    """Only backend-produced ToolMessages can force this final outcome."""
    recent = []
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            recent.append(message)
    if any(m.additional_kwargs.get("outcome_unknown") for m in recent):
        return "本次申请结果待核实，请稍后查看处理结果，避免重复提交。"
    if any(m.additional_kwargs.get("security_denied") for m in recent):
        return "请求中有操作未执行：权限或资源校验未通过。请检查当前账户及所选资源，或重新确认操作。"
    return None


async def main_agent(state, config=None) -> dict:
    """ReAct 推理步:调模型(带工具),累加 steps 与 token 消耗。
    注:streaming=True 下 ChatOpenAI 默认不回传 usage_metadata;usage 回传已在
    get_chat_model 统一收口(ch09 stream_usage=True),这里不再单独 bind。"""
    guarded = execution_guard(state)
    if guarded:
        return {"messages": [AIMessage(guarded)], "answer": guarded,
                "steps": state.get("steps", 0) + 1}
    specs = await registry.get_all_specs()   # ch08 现问现拿:内置+MCP 合并清单,每轮现拉
    from app.security.policy import policy_for
    definitions = [{"type": "function", "function": {
        "name": s.name, "description": s.description, "parameters": s.json_schema}}
        for s in specs if policy_for(s) is not None]
    model = get_chat_model(streaming=True).bind_tools(definitions)
    msgs = _agent_messages(state)
    _log_model_context(state, msgs)   # ch07:摘要+滑窗进日志,验收 tail -f 可见
    try:
        ai: AIMessage = await model.ainvoke(msgs, config)
    except ContextBudgetExceeded as exc:
        answer = "本次请求材料过长，无法在保留关键业务信息的前提下安全处理。请缩小问题范围，或联系人工客服。"
        logger.warning("agent_context_budget_exceeded conv=%s reason=%s",
                       state.get("conversation_id"), exc.reason)
        return {"messages": [AIMessage(answer)], "answer": answer,
                "steps": state.get("steps", 0) + 1,
                "suggested_actions": [{"type": "transfer_human"}],
                "trace": {"context_budget": {"reason": exc.reason, **exc.report.to_dict()}}}
    usage = ai.usage_metadata or {}
    used = usage.get("total_tokens", 0)
    # 前缀缓存命中数:system + tools 那段稳定前缀有没有真被复用,只能靠上游报的这个数看。
    # 上游不报就是 0,不代表没命中(得去服务商控制台看),别据此下结论。
    cached = (usage.get("input_token_details") or {}).get("cache_read", 0)
    logger.info("agent_step conv=%s step=%s input=%s cache_read=%s total=%s",
                state.get("conversation_id"), state.get("steps", 0) + 1,
                usage.get("input_tokens", 0), cached, used)
    return {"messages": [ai],
            "steps": state.get("steps", 0) + 1,
            "tokens_used": state.get("tokens_used", 0) + used}


async def agent_tools(state) -> dict:
    """All proposed tools, including refund-form proposals, pass the same security gate."""
    from app.security import approvals
    from app.security.context import SecurityContext
    from app.security.gate import decide
    from app.security.principal import Principal

    last = state["messages"][-1]
    cid, uid = state.get("conversation_id", 0), state.get("user_id", "")
    run_id = state.get("run_id", "")
    specs = {s.name: s for s in await registry.get_all_specs()}
    ticket_calls = [tc for tc in last.tool_calls if tc["name"] == "create_ticket"]
    first = ticket_calls[0] if ticket_calls else None
    grant = None
    response = None
    spec = specs.get("create_ticket")
    if first and spec and engine.validate_args(spec, dict(first.get("args") or {})) is None:
        ctx = SecurityContext(Principal(uid), cid, run_id, first["id"], mode=settings.tool_security_mode)
        decision = await decide(ctx, spec, first["args"])
        await approvals.audit_decision(ctx, spec, first["args"], decision)
        if decision.action == "ask":
            grant = await approvals.issue(ctx, spec.name, approvals.tool_identity(spec), first["args"])
            response = interrupt({"type": "confirm_ticket", "preview": first["args"],
                "approval_id": grant.approval_id, "expires_at": grant.expires_at.isoformat() + "Z",
                "tool_call_id": first["id"]})
    messages, actions = [], list(state.get("suggested_actions", []))
    denied_order = False
    for tc in last.tool_calls:
        if tc["name"] == "create_ticket" and tc is not first:
            messages.append(ToolMessage(content="一次只处理一个工单请求。", tool_call_id=tc["id"],
                                        name="create_ticket", status="error"))
            continue
        approval_id = None
        if tc is first and grant and isinstance(response, dict) and response.get("confirmed") is True:
            # Boolean alone never grants execution.
            if response.get("approval_id") == grant.approval_id:
                approval_id = grant.approval_id
        run = await engine.execute_tool_call(tc, cid, specs, user_id=uid,
                                             run_id=run_id, approval_id=approval_id,
            deny_note='用户已取消建单，本次不执行。' if tc is first and response is not None and not approval_id else None)
        messages.append(run.tool_message)
        if tc["name"] == "submit_refund" and run.ok:
            actions.append({"type": "refund_form", "draft": {
                "order_id": tc["args"]["order_id"], "reason": tc["args"].get("reason")}})
        if tc["name"] in {"query_order", "submit_refund"} and not run.ok:
            denied_order = True
    if denied_order:
        actions.append({"type": "select_order", "orders": await business.list_user_orders(uid)})
    return {"messages": messages, "suggested_actions": actions}


def resolve_answer(state) -> str:
    """最终答复:确定性节点写在 state['answer'];Agent 答复取最后一条 AIMessage 文本。"""
    guarded = execution_guard(state)
    if guarded:
        return guarded
    if state.get("answer"):
        return state["answer"]
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str):
                return content
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


async def log_node(state) -> dict:
    """日志记录:留痕 intent/route/trace(可观测地基),并落 MySQL 一条 assistant 消息(审计)。"""
    logger.info(
        "turn conv=%s intent=%s route=%s resolved=%r trace=%s",
        state.get("conversation_id"), state.get("intent"), state.get("route"),
        state.get("resolved_query"), state.get("trace", {}),
    )
    answer = resolve_answer(state)
    if state.get("conversation_id"):
        await repository.append_message(state["conversation_id"], "assistant",
                                        content=answer or None)
    return {}
