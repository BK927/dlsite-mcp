from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from pydantic import ValidationError

from dlsite_mcp.cache import TtlLruCache
from dlsite_mcp.contracts import ErrorCode, ServiceError, entity_envelope, success_result
from dlsite_mcp.cursor import CursorCodec
from dlsite_mcp.oauth import create_oauth_runtime
from dlsite_mcp.public_server import (
    PUBLIC_RESOURCE_TEMPLATES,
    PUBLIC_TOOL_NAMES,
    ServerDependencies,
    create_server,
)
from dlsite_mcp.server import _build_http_app, _HttpGateway, mcp


class FakeProvider:
    async def get_work(self, product_id: str, **options: object) -> dict[str, object]:
        return {"product_id": product_id, "work_name": "title"}

    async def get_maker(self, maker_id: str, **options: object) -> dict[str, object]:
        return {"maker_id": maker_id, "maker_name": "maker"}

    async def search_page(
        self, query: str, **options: object
    ) -> tuple[list[dict[str, object]], bool]:
        return ([{"product_id": "RJ123456", "title": "title"}], False)

    async def get_review_overview(self, product_id: str, **options: object) -> dict[str, object]:
        return {
            "product_id": product_id,
            "product_name": "title",
            "total_reviews": 1,
            "reviews_available": True,
        }

    async def get_review_page(self, product_id: str, **options: object) -> list[dict[str, object]]:
        return [
            {
                "review_id": "1",
                "reviewer_name": "reader",
                "title": "review title",
                "review": "review body",
                "review_truncated": False,
            }
        ]


def make_server():
    return create_server(
        ServerDependencies(
            provider=FakeProvider(),
            cursor=CursorCodec(b"x" * 32),
            cache=TtlLruCache(max_entries=8),
        )
    )


def legacy_server():
    """Use the same handlers with their previous unstructured registration."""
    server = make_server()
    for name in PUBLIC_TOOL_NAMES:
        server._tool_manager.get_tool(name).fn_metadata.output_schema = None
    return server


@pytest.mark.asyncio
async def test_exact_tool_surface_and_context_budget() -> None:
    server = make_server()
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_NAMES
    payload = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    # Include the typed output contract in the idle context budget.
    assert len(encoded) <= 10500
    input_payload = [{key: value for key, value in item.items() if key != "outputSchema"} for item in payload]
    assert len(json.dumps(input_payload, separators=(",", ":")).encode()) <= 3000
    assert all(len(json.dumps(item, separators=(",", ":")).encode()) <= 1000 for item in input_payload)
    for tool, item in zip(tools, payload, strict=True):
        assert len(tool.description.encode()) <= 180
        assert len(json.dumps(item, separators=(",", ":")).encode()) <= 4400
        assert tool.output_schema is not None
        Draft202012Validator.check_schema(tool.output_schema)
        assert set(tool.output_schema["required"]) == {
            "schema_version", "kind", "data", "items", "job", "page", "meta"
        }


@pytest.mark.asyncio
async def test_exact_resource_templates_and_no_prompts() -> None:
    server = make_server()
    assert (
        tuple(item.uri_template for item in server._resource_manager.list_templates())
        == PUBLIC_RESOURCE_TEMPLATES
    )
    assert await server.list_resources() == []
    assert await server.list_prompts() == []


@pytest.mark.asyncio
async def test_in_memory_mcp_handshake_and_cache_hint() -> None:
    server = make_server()
    async with Client(server) as client:
        listing = await client.list_tools()
        assert tuple(tool.name for tool in listing.tools) == PUBLIC_TOOL_NAMES
        assert listing.ttl_ms == 3_600_000
        templates = await client.list_resource_templates()
        assert (
            tuple(item.uri_template for item in templates.resource_templates)
            == PUBLIC_RESOURCE_TEMPLATES
        )


@pytest.mark.asyncio
async def test_tool_result_does_not_duplicate_structured_data() -> None:
    tool = make_server()._tool_manager.get_tool("dlsite_work_get")
    result = await tool.fn("RJ294126")
    assert result.structured_content["data"]["product_id"] == "RJ294126"
    assert "title" not in result.content[0].text


@pytest.mark.asyncio
async def test_review_result_is_structured_and_marks_user_text_untrusted() -> None:
    tool = make_server()._tool_manager.get_tool("dlsite_work_get")
    result = await tool.fn("RJ294126", view="reviews")
    assert result.structured_content["items"][0]["review"] == "review body"
    assert "items[].review" in result.structured_content["meta"]["untrusted_fields"]
    assert "review body" not in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("dlsite_work_get", {"work": "RJ294126", "view": "summary"}),
        ("dlsite_work_get", {"work": "RJ294126", "view": "details"}),
        ("dlsite_work_get", {"work": ["RJ294126", "RJ294127"]}),
        ("dlsite_work_get", {"work": "RJ294126", "view": "reviews"}),
        ("dlsite_search", {"query": "title"}),
        ("dlsite_maker_get", {"maker": "RG12345"}),
    ],
)
async def test_output_schema_validates_without_changing_wire_payload(
    name, arguments, monkeypatch
) -> None:
    monkeypatch.setattr("dlsite_mcp.contracts.utc_now", lambda: "2026-09-09T00:00:00Z")
    server = make_server()
    async with Client(legacy_server()) as client:
        original = await client.call_tool(name, arguments)
    async with Client(server) as client:
        listing = await client.list_tools()
        schema = next(tool.output_schema for tool in listing.tools if tool.name == name)
        result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert result.model_dump(by_alias=True) == original.model_dump(by_alias=True)
    Draft202012Validator(schema).validate(result.structured_content)
    assert "result" not in result.structured_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value"),
    [("page", None), ("has_more", "false"), ("product_id", 42)],
)
async def test_invalid_success_payload_is_rejected_by_schema_and_server(path, value) -> None:
    server = make_server()
    tool = server._tool_manager.get_tool("dlsite_work_get")
    malformed = await tool.fn("RJ294126")
    if path == "page":
        del malformed.structured_content["page"]
    elif path == "has_more":
        malformed.structured_content["page"][path] = value
    else:
        malformed.structured_content["data"][path] = value

    async def invalid_result(**arguments):
        return malformed

    tool.fn = invalid_result
    with pytest.raises(ValidationError):
        tool.fn_metadata.convert_result(malformed)
    async with Client(server) as client:
        listing = await client.list_tools()
        schema = next(t.output_schema for t in listing.tools if t.name == tool.name)
        assert not Draft202012Validator(schema).is_valid(malformed.structured_content)
        result = await client.call_tool(tool.name, {"work": "RJ294126"})
    assert result.is_error
    assert result.structured_content != malformed.structured_content


@pytest.mark.asyncio
async def test_service_error_keeps_existing_wire_contract_with_output_schema() -> None:
    server = make_server()
    tool = server._tool_manager.get_tool("dlsite_search")
    async with Client(legacy_server()) as client:
        original = await client.call_tool(tool.name, {"query": " "})
    async with Client(server) as client:
        result = await client.call_tool(tool.name, {"query": " "})
    assert result.is_error
    assert result.model_dump(by_alias=True) == original.model_dump(by_alias=True)
    assert result.structured_content == {
        "code": "INVALID_ARGUMENT",
        "message": "query must contain 1 to 200 characters.",
        "retryable": False,
        "schema_uri": "dlsite://schema/dlsite_search",
        "details": {},
    }


@pytest.mark.asyncio
async def test_provider_error_bypasses_success_schema_without_losing_details() -> None:
    class MissingProvider(FakeProvider):
        async def get_work(self, product_id, **options):
            raise ServiceError(ErrorCode.NOT_FOUND, "Missing work.", details={"reason": "gone"})

    server = create_server(ServerDependencies(
        provider=MissingProvider(), cursor=CursorCodec(b"x" * 32), cache=TtlLruCache(max_entries=8)
    ))
    async with Client(server) as client:
        result = await client.call_tool("dlsite_work_get", {"work": "RJ294126"})
    assert result.is_error
    assert result.structured_content["code"] == "NOT_FOUND"
    assert result.structured_content["details"] == {"reason": "gone", "product_id": "RJ294126"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("dlsite_work_get", {"work": "RJ294126", "view": "reviews", "limit": 1}),
        ("dlsite_search", {"query": "title", "limit": 1}),
    ],
)
async def test_populated_cursor_and_last_page_match_output_schema(name, arguments) -> None:
    class TwoItemProvider(FakeProvider):
        async def search_page(self, query, **options):
            return ([{"product_id": "RJ294126"}, {"product_id": "RJ294127"}], False)

        async def get_review_overview(self, product_id, **options):
            return {"product_name": "title", "total_reviews": 2, "reviews_available": True}

        async def get_review_page(self, product_id, **options):
            return [{"review_id": "1", "review": "one"}, {"review_id": "2", "review": "two"}]

    server = create_server(ServerDependencies(
        provider=TwoItemProvider(), cursor=CursorCodec(b"x" * 32), cache=TtlLruCache(max_entries=8)
    ))
    async with Client(server) as client:
        listing = await client.list_tools()
        schema = next(t.output_schema for t in listing.tools if t.name == name)
        first = await client.call_tool(name, arguments)
        assert not first.is_error
        Draft202012Validator(schema).validate(first.structured_content)
        page = first.structured_content["page"]
        assert page["returned"] == 1 and page["has_more"] is True
        assert isinstance(page["next_cursor"], str) and page["next_cursor"]
        last = await client.call_tool(name, {**arguments, "cursor": page["next_cursor"]})
        assert not last.is_error
        Draft202012Validator(schema).validate(last.structured_content)
        assert last.structured_content["page"] == {
            "returned": 1, "has_more": False, "next_cursor": None
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("max_bytes", [600, 1200])
async def test_budget_compaction_and_placeholder_match_output_schemas(max_bytes) -> None:
    server = make_server()
    for tool in await server.list_tools():
        # Several long extension fields force both partial records and the
        # existing {truncated: true} fallback without constraining provider data.
        envelope = entity_envelope({f"extra_{i}": "x" * 4000 for i in range(20)})
        result = success_result(envelope, "Returned.", max_bytes=max_bytes)
        assert len(json.dumps(result.structured_content, separators=(",", ":")).encode()) <= max_bytes
        if max_bytes == 600:
            assert result.structured_content["data"] == {"truncated": True}
        else:
            assert result.structured_content["data"]["truncation"]["fields_compacted"] is True
        Draft202012Validator(tool.output_schema).validate(result.structured_content)
        validated = server._tool_manager.get_tool(tool.name).fn_metadata.convert_result(result)
        assert validated is result


async def _asgi_call(app, path: str, headers: list[tuple[bytes, bytes]] | None = None):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers or [],
    }
    await app(scope, receive, send)
    return messages


@pytest.mark.asyncio
async def test_http_gateway_health_and_bearer_boundary() -> None:
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    gateway = _HttpGateway(
        downstream,
        mcp_path="/mcp",
        health_path="/healthz",
        access_token="s" * 32,
        allow_unauthenticated=False,
        oauth=None,
    )
    health = await _asgi_call(gateway, "/healthz")
    assert health[0]["status"] == 200
    denied = await _asgi_call(gateway, "/mcp")
    assert denied[0]["status"] == 401
    await _asgi_call(gateway, "/mcp", [(b"authorization", b"Bearer " + b"s" * 32)])
    assert called is True


@pytest.mark.asyncio
async def test_http_gateway_serves_oauth_discovery() -> None:
    async def downstream(scope, receive, send):
        raise AssertionError("discovery should not reach the MCP app")

    oauth = create_oauth_runtime(
        issuer="https://dlsite.example",
        resource="https://dlsite.example/mcp",
        scope="dlsite.read",
        login_secret="l" * 32,
        signing_secret="s" * 32,
        access_token="a" * 32,
        store="memory",
        project="",
        collection="test",
    )
    gateway = _HttpGateway(
        downstream,
        mcp_path="/mcp",
        health_path="/healthz",
        access_token="a" * 32,
        allow_unauthenticated=False,
        oauth=oauth,
    )
    messages = await _asgi_call(gateway, "/.well-known/oauth-authorization-server")
    assert messages[0]["status"] == 200
    payload = json.loads(messages[1]["body"])
    assert payload["issuer"] == "https://dlsite.example"
    assert payload["code_challenge_methods_supported"] == ["S256"]


def test_http_uses_shared_registry_and_two_mib_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.delenv("HTTP_MAX_BODY_BYTES", raising=False)
    gateway, host, port = _build_http_app()
    manager = gateway.app.routes[0].app.session_manager
    assert manager.app is mcp._lowlevel_server
    assert manager.max_request_body_size == 2_097_152
    assert host == "0.0.0.0"
    assert port == 8080
