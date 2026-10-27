from dataclasses import dataclass

from app.security.capabilities import Capability

@dataclass(frozen=True)
class ToolPolicy:
    source: str
    server: str | None
    capability: Capability
    resource: str = ""
    approval: bool = False


# Local identities are exact. A remote server's tool name or readOnly hint grants nothing.
POLICIES = {
    "query_faq": ToolPolicy("builtin", None, Capability.PUBLIC_READ),
    "query_product": ToolPolicy("builtin", None, Capability.PUBLIC_READ),
    "query_order": ToolPolicy("builtin", None, Capability.SENSITIVE_READ, "order"),
    "submit_refund": ToolPolicy("builtin", None, Capability.SENSITIVE_READ, "order"),
    "create_ticket": ToolPolicy("builtin", None, Capability.INTERNAL_WRITE, approval=True),
    "query_logistics": ToolPolicy("mcp", "logistics", Capability.SENSITIVE_READ, "tracking"),
    "query_warranty": ToolPolicy("mcp", "aftersales", Capability.SENSITIVE_READ, "order"),
    "query_return_status": ToolPolicy("mcp", "aftersales", Capability.SENSITIVE_READ, "order"),
}
HARD_DENY = frozenset({"execute_shell", "run_command", "execute_refund", "export_all_orders"})


def policy_for(spec):
    p = POLICIES.get(spec.name)
    if spec.name in HARD_DENY or not p or (spec.source, spec.mcp_server) != (p.source, p.server):
        return None
    expected = "write" if p.approval else "read"
    if spec.permission != expected:
        return None
    return p
