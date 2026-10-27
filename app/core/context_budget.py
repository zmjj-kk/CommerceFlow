"""Request-wide conservative accounting, immediately before transport.

UTF-8 byte counts deliberately overestimate ordinary byte-tokenized text. They
are estimates, not provider usage; the safety margin covers chat/tool framing.
No model context size is inferred from a model name.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)
_SUMMARY = "\n\n## 早前对话摘要"
# Only a closed vocabulary of social filler is known to be noncritical. Unknown
# prose stays protected, including policies/orders expressed in other languages.
_NONCRITICAL = re.compile(
    r"(?:(?:你好|您好|欢迎|谢谢|不客气|再见|早上好|晚上好|hello|hi|thanks|thank you|bye)"
    r"[\s,.!，。！?？~～]*)+", re.I)


@dataclass(frozen=True)
class BudgetReport:
    model: str
    context_limit: int
    output_reserve: int
    safety_margin: int
    components: dict[str, int]
    actions: tuple[str, ...]

    @property
    def input_tokens(self) -> int:
        return sum(self.components.values())

    @property
    def input_budget(self) -> int:
        return self.context_limit - self.output_reserve - self.safety_margin

    def to_dict(self) -> dict:
        return {**asdict(self), "input_tokens": self.input_tokens,
                "input_budget": self.input_budget, "estimator": "utf8_bytes_upper_estimate"}


class ContextBudgetExceeded(ValueError):
    def __init__(self, report: BudgetReport, reason: str = "protected_context_exceeds_budget"):
        self.report = report
        self.reason = reason
        super().__init__(f"{reason}: estimated input={report.input_tokens}, budget={report.input_budget}")


def _size(value) -> int:
    # Keep JSON escaping in the estimate, including tool-call argument strings.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _summary_size(content) -> int:
    if not isinstance(content, str) or not content.startswith(_SUMMARY):
        return 0
    ends = [content.find(marker) for marker in
            ("\n\n## 已检索到的知识证据", "\n\n## 退款判定任务")]
    end = min((i for i in ends if i >= 0), default=len(content))
    return _size(content[:end])


def enforce_request_budget(payload: dict, *, context_indices: set[int] | None = None,
                           allow_history_trim: bool = False) -> tuple[dict, BudgetReport]:
    """Copy and bound a complete Chat Completions payload; never mutate State.

    Only whole non-business historical turns are disposable. The current user
    turn, all tool exchanges, summary and business materials stay intact. There
    is no partial text truncation: unsafe or oversized material fails closed.
    """
    request = deepcopy(payload)
    model = str(request.get("model", settings.chat_model))
    limit = settings.chat_model_context_limits.get(model, settings.chat_context_limit)
    reserve = settings.chat_output_reserve
    margin = settings.chat_context_safety_margin
    actions: list[str] = []
    # A caller may request less output, never more than the reserved space.
    requested = [request.pop(key) for key in ("max_tokens", "max_completion_tokens") if key in request]
    if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in requested):
        raise ValueError("output token limits must be positive integers")
    request[settings.chat_output_token_parameter] = min(reserve, *requested) if requested else reserve
    context_indices = context_indices or set()
    # Parallel labels survive removal of old turns without exposing tags upstream.
    serialized = request.get("messages", [])
    latest_user = max((i for i, m in enumerate(serialized)
                       if m.get("role") == "user" and i not in context_indices), default=len(serialized))
    labels = ["business_material" if i in context_indices else
              "agent_runtime" if i > latest_user else "recent_dialogue"
              for i in range(len(serialized))]

    def report() -> BudgetReport:
        parts = dict.fromkeys(("fixed_prompt_and_tools", "history_summary", "recent_dialogue",
                               "business_material", "agent_runtime", "request_options"), 0)
        for message, label in zip(request.get("messages", []), labels):
            cost = _size(message) + 16  # conservative per-message framing
            role = message.get("role")
            if role in ("system", "developer"):
                parts["fixed_prompt_and_tools"] += cost
            elif label == "business_material":
                summary = _summary_size(message.get("content"))
                parts["history_summary"] += summary
                parts[label] += cost - summary
            elif role == "tool" or message.get("tool_calls"):
                parts["agent_runtime"] += cost
            else:
                parts[label] += cost
        parts["fixed_prompt_and_tools"] += _size(request.get("tools", [])) + 128
        # Includes schemas, tool_choice and any provider-specific request fields.
        parts["request_options"] = _size({k: v for k, v in request.items()
                                         if k not in ("messages", "tools")})
        return BudgetReport(model, limit, reserve, margin, parts, tuple(actions))

    def fits() -> bool:
        r = report()
        return r.input_budget > 0 and r.input_tokens <= r.input_budget

    logger.info("context_budget_before %s", json.dumps(report().to_dict(), ensure_ascii=False))

    if "messages" not in request:
        # Stateful Responses API input needs its own accountant; never bypass.
        raise ContextBudgetExceeded(report(), "unsupported_request_shape")

    extra = request.get("extra_body") or {}
    if set(extra) & {"messages", "tools", "model", "max_tokens", "max_completion_tokens",
                     "input", "previous_response_id"}:
        raise ContextBudgetExceeded(report(), "unsupported_request_override")
    for message in request["messages"]:
        content = message.get("content")
        if isinstance(content, list) and any(part.get("type") != "text" for part in content):
            raise ContextBudgetExceeded(report(), "unsupported_multimodal_context")

    # Remove only truly empty assistant messages without calls, reasoning or any
    # other fields. No repeated user messages or policy lines are deduplicated.
    if not fits():
        for i in range(len(request["messages"]) - 1, -1, -1):
            message = request["messages"][i]
            if (message.get("role") == "assistant" and not message.get("content")
                    and set(message) <= {"role", "content"}):
                del request["messages"][i]
                del labels[i]
                actions.append("remove_empty_assistant_message")

    if not fits() and allow_history_trim:
        messages = request["messages"]
        humans = [i for i, m in enumerate(messages)
                  if m.get("role") == "user" and labels[i] != "business_material"]
        removable = []
        for start, end in zip(humans, humans[1:]):
            turn = messages[start:end]
            if all(m.get("role") in ("user", "assistant") and not m.get("tool_calls")
                   and isinstance(m.get("content"), str)
                   and _NONCRITICAL.fullmatch(m["content"].strip())
                   for m in turn) and "business_material" not in labels[start:end]:
                removable.append((start, end))
        removed = 0
        for start, end in removable:
            if fits():
                break
            del messages[start - removed:end - removed]
            del labels[start - removed:end - removed]
            removed += end - start
            actions.append("remove_old_noncritical_turn")

    if not fits():
        # Lossless JSON whitespace compression only. Policy prose, identifiers,
        # condition chains, tool IDs/status/security flags are never truncated.
        for message in request["messages"]:
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            try:
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate JSON key")
                        result[key] = value
                    return result
                json.loads(message["content"], object_pairs_hook=unique_object)
                # Numeric lexemes can carry business precision; compact whitespace
                # outside strings directly instead of reserializing parsed values.
                compact = _compact_json(message["content"])
                if len(compact) < len(message["content"]):
                    message["content"] = compact
                    actions.append("compact_tool_json_losslessly")
            except (ValueError, RecursionError):
                continue
        # One bounded history pass and one compression pass; no iterative LLM
        # summarization, substring clipping, or unbounded retries.

    final = report()
    logger.log(logging.INFO if fits() else logging.WARNING,
               "context_budget %s", json.dumps(final.to_dict(), ensure_ascii=False))
    if not fits():
        raise ContextBudgetExceeded(final)
    return request, final


def _compact_json(text: str) -> str:
    out = []
    quoted = escaped = False
    for char in text:
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif char not in " \t\r\n":
            out.append(char)
    return "".join(out)
