"""Every tool execution passes local identity policy, resource authorization and audit."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from langchain_core.messages import ToolMessage

from app.config import settings
from app.db import repository
from app.security import approvals
from app.security.context import SecurityContext
from app.security.errors import SecurityError
from app.security.gate import Decision, decide
from app.security.principal import Principal
from app.security.policy import policy_for
from app.tools.registry import ToolSpec

logger = logging.getLogger(__name__)
_TRANSIENT = (TimeoutError, ConnectionError, httpx.TransportError)
_SUMMARY_LIMIT = 500
_RESERVED = {"user_id", "conversation_id", "approval_id", "run_id", "tool_call_id",
             "operation_id", "tool_identity", "principal", "mode", "confirmed"}


@dataclass
class ToolRun:
    tool_call_id: str
    name: str
    ok: bool
    tool_message: ToolMessage
    status: str
    retry_count: int = 0
    duration_ms: int = 0


def validate_args(spec: ToolSpec, args: dict) -> str | None:
    if not isinstance(args, dict) or _RESERVED.intersection(args):
        return "不允许提交身份或审批字段"
    if set(args) - set(spec.json_schema.get("properties", {})):
        return "存在未声明的参数"
    # No remote references: an untrusted schema must never trigger network resolution.
    if "$ref" in json.dumps(spec.json_schema):
        return "不支持引用型工具参数定义"
    Draft202012Validator.check_schema(spec.json_schema)
    err = best_match(Draft202012Validator(spec.json_schema).iter_errors(args))
    # Avoid reflecting secrets or arbitrary argument content in errors/logs.
    return "参数类型、必填项或范围不正确" if err else None


def _timeout_of(spec):
    return spec.timeout if spec.timeout is not None else (
        settings.mcp_tool_timeout if spec.source == "mcp" else settings.tool_default_timeout)


def _summarize(content):
    return content[:_SUMMARY_LIMIT]


def _format_content(spec, result):
    if isinstance(result, list) and result and all(isinstance(b, dict) for b in result):
        result = "\n".join(b.get("text", "") for b in result if b.get("type") == "text")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return json.dumps({'untrusted_tool_data': result[:16000]}, ensure_ascii=False) if spec.source == 'mcp' else result
    if spec.format_result and isinstance(result, dict):
        result = spec.format_result(result)
    if spec.source == "mcp":
        content = json.dumps({"untrusted_tool_data": result}, ensure_ascii=False, default=str)
        return content[:16000]
    return json.dumps(result, ensure_ascii=False, default=str)


async def _audit(cid, tcid, name, spec, args, result_summary, status, error_message, retries, duration):
    try:
        # Store digest and outcome, never credentials, descriptions or order payloads.
        await repository.insert_tool_audit(
            conversation_id=cid or None, tool_call_id=(tcid or "")[:64] or None,
            tool_name=name[:128], tool_source=spec.source if spec else "builtin",
            mcp_server=spec.mcp_server if spec else None,
            arguments={"sha256": approvals.argument_hash(args)},
            result_summary=None, status=status, error_message=error_message,
            retry_count=retries, duration_ms=duration)
    except Exception:
        logger.error("tool_audit_unavailable tool=%s status=%s", name[:128], status)


async def execute_tool_call(tool_call: dict, conversation_id: int, specs: dict[str, ToolSpec],
                            *, confirmed: bool = False, deny_note: str | None = None,
                            user_id: str = "", approval_id: str | None = None,
                            run_id: str = "", context: SecurityContext | None = None) -> ToolRun:
    name = str(tool_call.get("name") or "")[:128]
    tcid = str(tool_call.get("id") or "")[:64]
    started = time.monotonic()
    raw_args = tool_call.get("args")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    spec = specs.get(name)
    context = context or SecurityContext(Principal(user_id), conversation_id,
                                         run_id or uuid4().hex, tcid,
                                         mode=settings.tool_security_mode)
    # Explicit arguments must match context, never silently override a supplied principal.
    async def finish(ok, content, status, retries=0, code=None):
        run = ToolRun(tcid, name, ok, ToolMessage(
            content=content, tool_call_id=tcid, name=name or "unknown",
            status="success" if ok else "error", additional_kwargs={
                "security_denied": status == "权限拒绝", "outcome_unknown": code == "outcome_unknown"}), status, retries,
            int((time.monotonic() - started) * 1000))
        await _audit(conversation_id, tcid, name or "unknown", spec, args, None,
                     status, code, retries, run.duration_ms)
        return run

    if spec is None:
        # Unknown calls still get a durable policy decision where possible.
        from types import SimpleNamespace
        unknown = SimpleNamespace(name=name, source="builtin", mcp_server=None, json_schema={})
        try:
            await approvals.audit_decision(context, unknown, args, Decision("deny", "unknown_tool"))
        except Exception:
            logger.error("security_audit_unavailable")
        return await finish(False, "工具未执行：未知工具。", "权限拒绝", code="unknown_tool")
    try:
        if policy_for(spec) is None:
            decision = Decision("deny", "untrusted_tool_identity")
            await approvals.audit_decision(context, spec, args, decision)
            return await finish(False, "工具未执行：工具身份未受信任。", "权限拒绝", code=decision.reason_code)
        if raw_args is not None and not isinstance(raw_args, dict):
            return await finish(False, "参数必须为对象。", "校验拦下", code="invalid_args")
        verr = validate_args(spec, args)
        if verr:
            await approvals.audit_decision(context, spec, args, Decision("deny", "invalid_args"))
            return await finish(False, f"参数校验未通过：{verr}", "校验拦下", code="invalid_args")
        if (context.principal.user_id != user_id or context.conversation_id != conversation_id or
                context.tool_call_id != tcid):
            decision = Decision("deny", "context_mismatch")
        else:
            decision = await decide(context, spec, args)
        await approvals.audit_decision(context, spec, args, decision)  # failure closes execution
        if decision.action == "deny":
            return await finish(False, "工具未执行：权限或资源校验未通过。", "权限拒绝", code=decision.reason_code)
        if decision.action == "ask":
            if not approval_id:
                return await finish(False, deny_note or "工具未执行：需要对应操作的一次性审批。", "权限拒绝", code="approval_required")
            await approvals.check(approval_id, context, name, approvals.tool_identity(spec), args)
    except SecurityError as exc:
        return await finish(False, "工具未执行：审批无效或已使用。", "权限拒绝", code=exc.code)
    except Exception:
        logger.error("security_gate_unavailable tool=%s", name)
        return await finish(False, "工具未执行：安全校验暂时不可用。", "权限拒绝", code="security_unavailable")

    invoke_args = dict(args)
    if spec.inject_conversation:
        invoke_args["conversation_id"] = conversation_id
    if spec.inject_user_id:
        invoke_args["user_id"] = user_id
    if spec.permission == "write":
        invoke_args.update(approval_id=approval_id, run_id=context.run_id,
                           tool_call_id=tcid, tool_identity=approvals.tool_identity(spec))
    retries = 0 if spec.permission == "write" else (
        settings.tool_max_retries if spec.max_retries is None else spec.max_retries)
    attempt = 0
    while True:
        try:
            result = await asyncio.wait_for(spec.tool.ainvoke(invoke_args), timeout=_timeout_of(spec))
            content = _format_content(spec, result)
            # Business denials cannot become successful ToolMessages.
            is_error = isinstance(result, dict) and bool(result.get("error"))
            return await finish(not is_error, content, "权限拒绝" if is_error else "成功",
                                attempt, "business_denied" if is_error else None)
        except _TRANSIENT as exc:
            if attempt < retries:
                attempt += 1
                await asyncio.sleep(0.2 * attempt)
                continue
            status = "超时" if isinstance(exc, TimeoutError) else "失败"
            message = ("执行结果待核实，请查询原操作状态，勿创建新操作重试。" if spec.permission == "write"
                       else "工具暂时不可用，请稍后再试或如实告知用户。")
            return await finish(False, message, status, attempt, "outcome_unknown" if spec.permission == "write" else "transient_error")
        except SecurityError as exc:
            return await finish(False, "工具未执行：资源或审批校验失败。", "权限拒绝", attempt, exc.code)
        except Exception:
            logger.error("tool_execution_failed tool=%s", name)
            return await finish(False, "工具暂时不可用，请如实告知用户，不要编造结果。", "失败", attempt, "execution_failed")
