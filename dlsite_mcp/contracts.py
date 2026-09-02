"""Stable, compact MCP response contracts."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from mcp.types import CallToolResult, TextContent

SCHEMA_VERSION = "1"


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    NOT_FOUND = "NOT_FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    TIMEOUT = "TIMEOUT"


class ServiceError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        schema_uri: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.schema_uri = schema_uri
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _meta(
    *,
    canonical_uri: str | None = None,
    provider: str = "dlsite-async",
    warnings: list[str] | None = None,
    untrusted_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "canonical_uri": canonical_uri,
        "source": "dlsite",
        "provider": provider,
        "retrieved_at": utc_now(),
        "fresh_until": None,
        "quota_cost": None,
        "warnings": warnings or [],
        "untrusted_fields": untrusted_fields or [],
    }


def entity_envelope(
    data: dict[str, Any],
    *,
    canonical_uri: str | None = None,
    provider: str = "dlsite-async",
    warnings: list[str] | None = None,
    untrusted_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "entity",
        "data": data,
        "items": [],
        "job": {},
        "page": {"returned": 0, "has_more": False, "next_cursor": None},
        "meta": _meta(
            canonical_uri=canonical_uri,
            provider=provider,
            warnings=warnings,
            untrusted_fields=untrusted_fields,
        ),
    }


def collection_envelope(
    items: list[dict[str, Any]],
    *,
    next_cursor: str | None = None,
    data: dict[str, Any] | None = None,
    canonical_uri: str | None = None,
    provider: str = "dlsite-async",
    warnings: list[str] | None = None,
    untrusted_fields: list[str] | None = None,
) -> dict[str, Any]:
    if len(items) > 100:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "A result page cannot exceed 100 items.")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "collection",
        "data": data or {},
        "items": items,
        "job": {},
        "page": {
            "returned": len(items),
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        },
        "meta": _meta(
            canonical_uri=canonical_uri,
            provider=provider,
            warnings=warnings,
            untrusted_fields=untrusted_fields,
        ),
    }


def compact_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _bounded(value: Any, max_chars: int) -> tuple[Any, bool]:
    remaining = max(256, max_chars)
    truncated = False

    def walk(item: Any) -> Any:
        nonlocal remaining, truncated
        if isinstance(item, str):
            if len(item) <= remaining:
                remaining -= len(item)
                return item
            kept = max(0, remaining)
            remaining = 0
            truncated = True
            return item[:kept] + "…"
        if isinstance(item, list):
            output = []
            for child in item:
                if remaining <= 0:
                    truncated = True
                    break
                output.append(walk(child))
            return output
        if isinstance(item, dict):
            output = {}
            for key, child in item.items():
                if remaining <= 0:
                    truncated = True
                    break
                output[key] = walk(child)
            return output
        return item

    return walk(value), truncated


def enforce_budget(
    envelope: dict[str, Any],
    *,
    default_bytes: int = 12 * 1024,
    hard_bytes: int = 32 * 1024,
) -> dict[str, Any]:
    value = copy.deepcopy(envelope)
    if compact_size(value) <= default_bytes:
        return value

    warnings = value["meta"]["warnings"]
    warnings.append("Result reduced to the 12 KiB MCP default budget.")
    original_count = len(value["items"])
    if value["items"]:
        per_item = max(200, default_bytes // max(2, len(value["items"])))
        value["items"] = [_bounded(item, per_item)[0] for item in value["items"]]
    value["data"], data_cut = _bounded(value["data"], default_bytes // 3)

    while value["items"] and compact_size(value) > default_bytes:
        value["items"].pop()
    value["page"]["returned"] = len(value["items"])
    value["data"]["truncation"] = {
        "original_items": original_count,
        "returned_items": len(value["items"]),
        "fields_compacted": data_cut,
    }
    if compact_size(value) > default_bytes:
        canonical_uri = value["meta"].get("canonical_uri")
        value = entity_envelope(
            {"truncated": True},
            canonical_uri=canonical_uri,
            warnings=["Result exceeded 12 KiB and was reduced to a placeholder."],
        )
    if compact_size(value) > min(default_bytes, hard_bytes):
        raise ServiceError(ErrorCode.PROVIDER_UNAVAILABLE, "Unable to enforce the result limit.")
    return value


def success_result(
    envelope: dict[str, Any], summary: str, *, max_bytes: int = 12 * 1024
) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=summary[:300])],
        structuredContent=enforce_budget(envelope, default_bytes=max_bytes),
    )


def error_result(error: ServiceError) -> CallToolResult:
    payload = {
        "code": error.code.value,
        "message": error.message,
        "retryable": error.retryable,
        "schema_uri": error.schema_uri,
        "details": error.details,
    }
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=f"{error.code.value}: {error.message}"[:300])],
        structuredContent=payload,
    )
