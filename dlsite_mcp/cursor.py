"""HMAC-signed stateless cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import ErrorCode, ServiceError


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The cursor is not valid base64url.") from exc


def filter_digest(filters: dict[str, Any]) -> str:
    raw = json.dumps(filters, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class CursorCodec:
    secret: bytes
    ttl_seconds: int = 86_400
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("cursor secret must contain at least 32 bytes")
        if self.ttl_seconds < 1:
            raise ValueError("cursor ttl must be positive")

    def encode(self, *, scope: str, filters: dict[str, Any], state: dict[str, Any]) -> str:
        payload = {
            "v": 1,
            "scope": scope,
            "filter": filter_digest(filters),
            "exp": int(self.clock()) + self.ttl_seconds,
            "state": state,
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self.secret, body, hashlib.sha256).digest()
        return f"{_encode(body)}.{_encode(signature)}"

    def decode(self, cursor: str, *, scope: str, filters: dict[str, Any]) -> dict[str, Any]:
        try:
            body_part, signature_part = cursor.split(".", 1)
        except ValueError as exc:
            raise ServiceError(
                ErrorCode.CURSOR_MISMATCH, "The cursor has an invalid shape."
            ) from exc
        body = _decode(body_part)
        supplied = _decode(signature_part)
        expected = hmac.new(self.secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The cursor signature is invalid.")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The cursor payload is invalid.") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("scope") != scope:
            raise ServiceError(
                ErrorCode.CURSOR_MISMATCH, "The cursor belongs to another operation."
            )
        if payload.get("filter") != filter_digest(filters):
            raise ServiceError(
                ErrorCode.CURSOR_MISMATCH, "The cursor filters do not match this request."
            )
        if int(payload.get("exp", 0)) <= int(self.clock()):
            raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The cursor has expired.", retryable=True)
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The cursor state is invalid.")
        return state
