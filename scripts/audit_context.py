"""Fail CI when the idle DLsite MCP context surface grows unexpectedly."""

from __future__ import annotations

import asyncio
import json

from dlsite_mcp.server import mcp


async def main() -> None:
    tools = await mcp.list_tools()
    payload = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    sizes = {
        tool.name: len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
        for tool, item in zip(tools, payload, strict=True)
    }
    total = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    errors = []
    if total > 3000:
        errors.append(f"tools/list is {total} bytes; limit is 3000")
    for tool in tools:
        if len(tool.description.encode()) > 180:
            errors.append(f"{tool.name} description exceeds 180 bytes")
    for name, size in sizes.items():
        if size > 1000:
            errors.append(f"{name} is {size} bytes; limit is 1000")
    print(json.dumps({"total_bytes": total, "tool_bytes": sizes}, indent=2))
    if errors:
        raise SystemExit("\n".join(errors))


if __name__ == "__main__":
    asyncio.run(main())
