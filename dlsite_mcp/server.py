"""Shared stdio and Streamable HTTP runtime."""

from __future__ import annotations

import json
import os
import secrets
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .cache import TtlLruCache
from .cursor import CursorCodec
from .oauth import OAuthRuntime, create_oauth_runtime
from .provider import DlsiteProvider
from .public_server import ServerDependencies, create_server


def _read_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _read_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _read_path_env(name: str, default: str) -> str:
    path = os.getenv(name, default).strip()
    if not path.startswith("/") or "?" in path or "#" in path:
        raise RuntimeError(f"{name} must be an absolute URL path")
    return path.rstrip("/") or "/"


def _comma_values(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _transport_security(host: str) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = _comma_values("MCP_ALLOWED_HOSTS")
    allowed_origins = _comma_values("MCP_ALLOWED_ORIGINS")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if public_base_url:
        parsed = urlsplit(public_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTPS URL")
        if parsed.netloc not in allowed_hosts:
            allowed_hosts.append(parsed.netloc)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in allowed_origins:
            allowed_origins.append(origin)
    if host in {"127.0.0.1", "localhost"}:
        allowed_hosts.extend(
            item for item in ("127.0.0.1:*", "localhost:*") if item not in allowed_hosts
        )
        allowed_origins.extend(
            item
            for item in ("http://127.0.0.1:*", "http://localhost:*")
            if item not in allowed_origins
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _oauth_from_env(access_token: str) -> OAuthRuntime | None:
    if not _read_bool_env("MCP_OAUTH_ENABLED", False):
        return None
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("OAuth requires PUBLIC_BASE_URL")
    mcp_path = _read_path_env("MCP_PATH", "/mcp")
    return create_oauth_runtime(
        issuer=os.getenv("MCP_OAUTH_ISSUER", base).strip(),
        resource=os.getenv("MCP_OAUTH_RESOURCE", f"{base}{mcp_path}").strip(),
        scope=os.getenv("MCP_OAUTH_SCOPE", "dlsite.read").strip(),
        login_secret=os.getenv("MCP_OAUTH_LOGIN_SECRET", "").strip(),
        signing_secret=os.getenv("MCP_OAUTH_SIGNING_SECRET", "").strip(),
        access_token=access_token,
        store=os.getenv("MCP_OAUTH_STORE", "memory").strip().lower(),
        project=os.getenv("GCP_PROJECT", "").strip(),
        collection=os.getenv("MCP_OAUTH_CODE_COLLECTION", "dlsite_oauth_codes").strip(),
    )


def _dependencies() -> ServerDependencies:
    raw_secret = os.getenv("DLSITE_CURSOR_SECRET", "").encode()
    if not raw_secret:
        raw_secret = os.getenv("MCP_ACCESS_TOKEN", "").encode()
    if len(raw_secret) < 32:
        raw_secret = secrets.token_bytes(32)
    return ServerDependencies(
        provider=DlsiteProvider(
            timeout_seconds=_read_int_env("DLSITE_TIMEOUT_SECONDS", 25, 5, 120)
        ),
        cursor=CursorCodec(
            raw_secret,
            ttl_seconds=_read_int_env("DLSITE_CURSOR_TTL_SECONDS", 86_400, 60, 604_800),
        ),
        cache=TtlLruCache(
            max_entries=_read_int_env("DLSITE_CACHE_MAX_ENTRIES", 256, 1, 4096),
            ttl_seconds=_read_int_env("DLSITE_CACHE_TTL_SECONDS", 600, 1, 86_400),
        ),
        max_result_bytes=_read_int_env("DLSITE_MAX_RESULT_BYTES", 12_288, 4_096, 32_768),
    )


_access_token = os.getenv("MCP_ACCESS_TOKEN", "").strip()
_oauth = _oauth_from_env(_access_token)
mcp = create_server(_dependencies(), _oauth)


class _HttpGateway:
    def __init__(
        self,
        app: Any,
        *,
        mcp_path: str,
        health_path: str,
        access_token: str,
        allow_unauthenticated: bool,
        oauth: OAuthRuntime | None,
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.health_path = health_path
        self.access_token = access_token
        self.allow_unauthenticated = allow_unauthenticated
        self.oauth = oauth

    def _authorized(self, scope: dict[str, Any]) -> bool:
        if self.allow_unauthenticated:
            return True
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw = headers.get(b"authorization", b"")
        if not raw.startswith(b"Bearer "):
            return False
        supplied = raw[len(b"Bearer ") :].decode(errors="ignore")
        return secrets.compare_digest(supplied, self.access_token)

    @staticmethod
    async def _json_response(
        send: Any,
        status: int,
        payload: dict[str, Any],
        *,
        head: bool = False,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ]
        headers.extend(extra_headers or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if head else body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path == self.health_path:
            await self._json_response(
                send,
                200,
                {"ok": True, "service": "dlsite-mcp", "version": __version__},
                head=method == "HEAD",
            )
            return
        if path == "/":
            await self._json_response(
                send,
                200,
                {
                    "service": "dlsite-mcp",
                    "version": __version__,
                    "transport": "streamable-http",
                    "mcpPath": self.mcp_path,
                    "readOnly": True,
                    "authentication": "oauth2+static-bearer"
                    if self.oauth
                    else ("static-bearer" if self.access_token else "none"),
                },
                head=method == "HEAD",
            )
            return
        if (
            self.oauth
            and path == "/.well-known/oauth-authorization-server"
            and method in {"GET", "HEAD"}
        ):
            await self._json_response(
                send, 200, self.oauth.provider.authorization_metadata(), head=method == "HEAD"
            )
            return
        if (
            self.oauth
            and path
            in {
                "/.well-known/oauth-protected-resource",
                f"/.well-known/oauth-protected-resource{self.mcp_path}",
            }
            and method in {"GET", "HEAD"}
        ):
            await self._json_response(
                send,
                200,
                {
                    "resource": self.oauth.provider.resource,
                    "authorization_servers": [self.oauth.provider.issuer],
                    "scopes_supported": [self.oauth.provider.scope],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "DLsite MCP",
                },
                head=method == "HEAD",
            )
            return
        if (
            not self.oauth
            and path in {self.mcp_path, f"{self.mcp_path}/"}
            and not self._authorized(scope)
        ):
            await self._json_response(
                send,
                401,
                {"error": "unauthorized"},
                head=method == "HEAD",
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return
        await self.app(scope, receive, send)


def _build_http_app() -> tuple[Any, str, int]:
    host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _read_int_env("PORT", 8080, 1, 65_535)
    mcp_path = _read_path_env("MCP_PATH", "/mcp")
    health_path = _read_path_env("HEALTH_PATH", "/healthz")
    access_token = os.getenv("MCP_ACCESS_TOKEN", "").strip()
    allow_unauthenticated = _read_bool_env("MCP_ALLOW_UNAUTHENTICATED", False)
    if not allow_unauthenticated and not _oauth and len(access_token) < 32:
        raise RuntimeError(
            "HTTP mode requires MCP_ACCESS_TOKEN with at least 32 characters, "
            "unless MCP_ALLOW_UNAUTHENTICATED=true"
        )
    app = mcp.streamable_http_app(
        streamable_http_path=mcp_path,
        stateless_http=True,
        max_request_body_size=_read_int_env("HTTP_MAX_BODY_BYTES", 2_097_152, 1024, 16_777_216),
        transport_security=_transport_security(host),
        host=host,
    )
    return (
        _HttpGateway(
            app,
            mcp_path=mcp_path,
            health_path=health_path,
            access_token=access_token,
            allow_unauthenticated=allow_unauthenticated,
            oauth=_oauth,
        ),
        host,
        port,
    )


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
        return
    if transport in {"http", "streamable-http"}:
        import uvicorn

        app, host, port = _build_http_app()
        uvicorn.run(app, host=host, port=port, log_level="info")
        return
    raise RuntimeError("MCP_TRANSPORT must be stdio, http, or streamable-http")


if __name__ == "__main__":
    main()
