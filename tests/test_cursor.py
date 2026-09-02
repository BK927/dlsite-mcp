from __future__ import annotations

import pytest

from dlsite_mcp.contracts import ErrorCode, ServiceError
from dlsite_mcp.cursor import CursorCodec


def test_cursor_round_trip_and_filter_binding() -> None:
    codec = CursorCodec(b"x" * 32, clock=lambda: 100)
    cursor = codec.encode(scope="search", filters={"query": "asmr"}, state={"page": 2})
    assert codec.decode(cursor, scope="search", filters={"query": "asmr"}) == {"page": 2}
    with pytest.raises(ServiceError) as caught:
        codec.decode(cursor, scope="search", filters={"query": "game"})
    assert caught.value.code is ErrorCode.CURSOR_MISMATCH


def test_cursor_rejects_tampering_and_expiry() -> None:
    valid = CursorCodec(b"x" * 32, ttl_seconds=10, clock=lambda: 100).encode(
        scope="search", filters={}, state={"page": 1}
    )
    with pytest.raises(ServiceError):
        CursorCodec(b"x" * 32, clock=lambda: 100).decode(valid + "a", scope="search", filters={})
    with pytest.raises(ServiceError) as caught:
        CursorCodec(b"x" * 32, clock=lambda: 111).decode(valid, scope="search", filters={})
    assert caught.value.retryable is True
