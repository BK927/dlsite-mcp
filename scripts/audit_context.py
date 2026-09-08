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
    input_payload = [{key: value for key, value in item.items() if key != "outputSchema"} for item in payload]
    input_sizes = [len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) for item in input_payload]
    input_total = len(json.dumps(input_payload, ensure_ascii=False, separators=(",", ":")).encode())
    errors = []
    # Typed output contracts share the same bounded idle context budget.
    if total > 10500:
        errors.append(f"tools/list is {total} bytes; limit is 10500")
    if input_total > 3000 or any(size > 1000 for size in input_sizes):
        errors.append("Descriptions, inputs and annotations exceed their original 3000/1000-byte limits")
    for tool in tools:
        if tool.output_schema is None:
            errors.append(f"{tool.name} is missing outputSchema")
        if len(tool.description.encode()) > 180:
            errors.append(f"{tool.name} description exceeds 180 bytes")
    for name, size in sizes.items():
        if size > 4400:
            errors.append(f"{name} is {size} bytes; limit is 4400")
    print(json.dumps({"total_bytes": total, "tool_bytes": sizes}, indent=2))
    if errors:
        raise SystemExit("\n".join(errors))


if __name__ == "__main__":
    asyncio.run(main())
