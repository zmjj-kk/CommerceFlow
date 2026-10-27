"""Only pinned remote contracts and fixed configured endpoints enter the registry."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import httpx
from urllib.parse import urlsplit

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import settings
from app.tools import registry
from app.tools.registry import ToolSpec

logger = logging.getLogger(__name__)
_client = None
_client_config = None
_RESOURCE_ARGS = {"query_logistics": ("logistics", "tracking_no"),
                  "query_warranty": ("aftersales", "order_id"),
                  "query_return_status": ("aftersales", "order_id")}
_DESCRIPTIONS = {"query_logistics": "查询本人订单的物流状态。tracking_no 必须来自已授权的订单。",
                 "query_warranty": "查询本人订单的保修状态。",
                 "query_return_status": "查询本人订单的退货申请状态。"}


def schema_fingerprint(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _translate(mapping, code):
    return mapping.get(code, code)


def _fmt_logistics(d):
    return {"tracking_no": d.get("tracking_no"),
            "status": _translate({"PICKED_UP": "已揽件", "IN_TRANSIT": "运输中",
                                 "DELIVERING": "派送中", "DELIVERED": "已签收"}, d.get("status_code")),
            "current_city": d.get("current_city"), "trace": d.get("trace")}


def _fmt_warranty(d):
    return {"order_id": d.get("order_id"),
            "warranty": _translate({"IN_WARRANTY": "在保", "EXPIRED": "已过保"}, d.get("warranty_code")),
            "warranty_until": d.get("warranty_until")}


def _fmt_return(d):
    return {"order_id": d.get("order_id"),
            "return_status": _translate({"AUDITING": "审核中", "RETURNING": "退货中",
                                         "REFUNDED": "已退款", "NONE": "无退货记录"}, d.get("return_code")),
            "updated_at": d.get("updated_at")}


FORMATTERS = {"query_logistics": _fmt_logistics, "query_warranty": _fmt_warranty,
              "query_return_status": _fmt_return}


def _endpoint(server):
    return {"logistics": settings.mcp_logistics_url, "aftersales": settings.mcp_aftersales_url}[server]


def _connection(server):
    url = _endpoint(server)
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid_mcp_endpoint")
    token = getattr(settings, f"mcp_{server}_token").get_secret_value()
    local = parsed.hostname in {"127.0.0.1", "::1"}
    if parsed.scheme == "http":
        if not settings.mcp_allow_local_http or not local or parsed.port not in {8101, 8102} or parsed.path.rstrip("/") != "/mcp":
            raise ValueError("mcp_tls_required")
    elif parsed.scheme != "https" or not token:
        raise ValueError("mcp_authenticated_tls_required")
    result = {"transport": "streamable_http", "url": url}
    if token:
        result["headers"] = {"Authorization": f"Bearer {token}"}
    return result


def _connections():
    result = {}
    for server in ("logistics", "aftersales"):
        try:
            result[server] = _connection(server)
        except ValueError:
            logger.warning("mcp_connection_rejected server=%s", server)
    return result


def get_client():
    global _client, _client_config
    connections = _connections()
    # Credentials/endpoints rotate the client without ever entering tool metadata.
    digest = schema_fingerprint(connections)
    if _client is None or digest != _client_config:
        secured = {server: {**connection, "httpx_client_factory": _http_client_factory(connection["url"])}
                   for server, connection in connections.items()}
        _client = MultiServerMCPClient(secured, handle_tool_errors=False)
        _client_config = digest
    return _client


async def _get_tools_of(*, server_name):
    return await get_client().get_tools(server_name=server_name)


def trusted_spec(spec):
    expected = _RESOURCE_ARGS.get(spec.name)
    if not expected or spec.mcp_server != expected[0] or spec.source != "mcp":
        return False
    try:
        _connection(spec.mcp_server)
    except ValueError:
        return False
    return (spec.endpoint == _endpoint(spec.mcp_server) and
            spec.schema_fingerprint == schema_fingerprint(spec.json_schema) and
            settings.mcp_schema_pins.get(f"{spec.mcp_server}/{spec.name}") == spec.schema_fingerprint)


async def fetch_mcp_specs() -> list[ToolSpec]:
    specs = []
    for server in _connections():
        try:
            tools = await asyncio.wait_for(_get_tools_of(server_name=server), settings.mcp_tool_timeout)
            for tool in tools:
                expected = _RESOURCE_ARGS.get(tool.name)
                if not expected or expected[0] != server:
                    logger.warning("mcp_candidate_rejected server=%s", server)
                    continue
                spec = registry.spec_from_langchain_tool(tool, source="mcp", mcp_server=server,
                    format_result=FORMATTERS[tool.name])
                spec.endpoint = _endpoint(server)
                spec.schema_fingerprint = schema_fingerprint(spec.json_schema)
                if not trusted_spec(spec):
                    logger.warning("mcp_contract_not_pinned server=%s tool=%s", server, tool.name)
                    continue
                # No user-supplied URL, credential, principal or undeclared batch argument.
                prop = spec.json_schema.get("properties", {})
                if (set(prop) != {expected[1]} or prop[expected[1]].get("type") != "string" or
                        set(spec.json_schema.get("required", [])) != {expected[1]}):
                    continue
                spec.description = _DESCRIPTIONS[tool.name]
                specs.append(spec)
        except Exception:
            logger.warning("mcp_discovery_unavailable server=%s", server)
    return specs


def _http_client_factory(endpoint: str):
    expected = httpx.URL(endpoint)
    async def check_request(request):
        if request.url != expected:
            raise httpx.RequestError("mcp_destination_changed", request=request)
    async def check_response(response):
        if response.is_redirect:
            raise httpx.RequestError("mcp_redirect_rejected", request=response.request)
    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, auth=auth,
            timeout=httpx.Timeout(settings.mcp_tool_timeout),
            follow_redirects=False, trust_env=False,
            event_hooks={"request": [check_request], "response": [check_response]})
    return factory
