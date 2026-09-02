from __future__ import annotations

import pytest

from dlsite_mcp.oauth import (
    CHATGPT_STABLE_CLIENT_ID,
    MemoryAuthorizationCodeStore,
    PersonalOAuthProvider,
)


def provider() -> PersonalOAuthProvider:
    return PersonalOAuthProvider(
        issuer="https://example.com",
        resource="https://example.com/mcp",
        scope="dlsite.read",
        login_secret="l" * 32,
        signing_secret="s" * 32,
        access_token="a" * 32,
        code_store=MemoryAuthorizationCodeStore(),
    )


@pytest.mark.asyncio
async def test_oauth_allows_chatgpt_clients_only() -> None:
    instance = provider()
    assert await instance.get_client(CHATGPT_STABLE_CLIENT_ID) is not None
    assert await instance.get_client("https://chatgpt.com/oauth/example/client.json") is not None
    assert await instance.get_client("https://attacker.example/client.json") is None


@pytest.mark.asyncio
async def test_static_bearer_is_valid_oauth_access_token() -> None:
    token = await provider().load_access_token("a" * 32)
    assert token is not None
    assert token.scopes == ["dlsite.read"]
