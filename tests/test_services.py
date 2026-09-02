from __future__ import annotations

import pytest

from dlsite_mcp.cache import TtlLruCache
from dlsite_mcp.contracts import ErrorCode, ServiceError
from dlsite_mcp.cursor import CursorCodec
from dlsite_mcp.services import DlsiteService


class FakeProvider:
    def __init__(self) -> None:
        self.work_calls = 0

    async def get_work(self, product_id: str, **options: object) -> dict[str, object]:
        self.work_calls += 1
        return {"product_id": product_id, "work_name": "untrusted", "description": "text"}

    async def get_maker(self, maker_id: str, **options: object) -> dict[str, object]:
        return {"maker_id": maker_id, "maker_name": "untrusted"}

    async def search_page(
        self, query: str, **options: object
    ) -> tuple[list[dict[str, object]], bool]:
        page = int(options["page"])
        return ([{"product_id": f"RJ{page:02}{index:05}"} for index in range(30)], page < 2)


def service() -> tuple[DlsiteService, FakeProvider]:
    provider = FakeProvider()
    return (
        DlsiteService(provider, TtlLruCache(max_entries=8), CursorCodec(b"x" * 32)),
        provider,
    )


@pytest.mark.asyncio
async def test_work_get_caches_and_marks_untrusted_fields() -> None:
    instance, provider = service()
    first = await instance.work_get("RJ294126", "details", "ja_JP", "ko_KR", 4000)
    second = await instance.work_get("RJ294126", "details", "ja_JP", "ko_KR", 4000)
    assert first["data"]["product_id"] == "RJ294126"
    assert "data.description" in first["meta"]["untrusted_fields"]
    assert second["data"] == first["data"]
    assert provider.work_calls == 1


@pytest.mark.asyncio
async def test_details_batch_is_bounded() -> None:
    instance, _ = service()
    with pytest.raises(ServiceError) as caught:
        await instance.work_get(
            [f"RJ{index:06}" for index in range(6)], "details", "ja_JP", "ko_KR", 4000
        )
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_search_cursor_continues_without_skipping() -> None:
    instance, _ = service()
    first = await instance.search("asmr", "maniax", "", 10, "ja_JP", "ko_KR")
    second = await instance.search(
        "asmr", "maniax", first["page"]["next_cursor"], 10, "ja_JP", "ko_KR"
    )
    assert first["page"]["returned"] == 10
    assert second["items"][0]["product_id"] == "RJ0100010"
    assert "items[].title" in first["meta"]["untrusted_fields"]


@pytest.mark.asyncio
async def test_search_cursor_rejects_changed_query() -> None:
    instance, _ = service()
    first = await instance.search("asmr", "maniax", "", 10, "ja_JP", "ko_KR")
    with pytest.raises(ServiceError) as caught:
        await instance.search("game", "maniax", first["page"]["next_cursor"], 10, "ja_JP", "ko_KR")
    assert caught.value.code is ErrorCode.CURSOR_MISMATCH
