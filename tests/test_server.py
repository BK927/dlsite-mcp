from __future__ import annotations

import json

import pytest
from mcp import Client

from dlsite_mcp.cache import TtlLruCache
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


@pytest.mark.asyncio
async def test_exact_tool_surface_and_context_budget() -> None:
    server = make_server()
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_NAMES
    payload = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= 3000
    for tool, item in zip(tools, payload, strict=True):
        assert len(tool.description.encode()) <= 180
        assert len(json.dumps(item, separators=(",", ":")).encode()) <= 1000


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
