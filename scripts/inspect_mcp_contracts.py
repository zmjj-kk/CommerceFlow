"""Print candidate fingerprints for human review. This does not authorize any tool."""
import asyncio
import json
from app.tools import mcp_client, registry


async def main():
    result = {}
    for server in mcp_client._connections():
        tools = await asyncio.wait_for(mcp_client._get_tools_of(server_name=server), 10)
        for tool in tools:
            expected = mcp_client._RESOURCE_ARGS.get(tool.name)
            if expected and expected[0] == server:
                schema = registry._json_schema_of(tool)
                print(json.dumps({"server": server, "tool": tool.name, "schema": schema}, ensure_ascii=False))
                result[f"{server}/{tool.name}"] = mcp_client.schema_fingerprint(schema)
    print("Review each contract before configuring MCP_SCHEMA_PINS:")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
