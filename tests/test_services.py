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

    async def get_review_overview(self, product_id: str, **options: object) -> dict[str, object]:
        return {
            "product_id": product_id,
            "product_name": "untrusted work",
            "total_reviews": 10_000,
            "reviews_available": True,
        }

    async def get_review_page(self, product_id: str, **options: object) -> list[dict[str, object]]:
        page = int(options["page"])
        limit = int(options["limit"])
        start = (page - 1) * limit
        return [
            {
                "review_id": str(index),
                "reviewer_name": "untrusted reviewer",
                "title": f"review {index}",
                "review": "untrusted body",
                "review_truncated": False,
            }
            for index in range(start, min(start + limit, 10_000))
        ]


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
async def test_mixed_batch_identifies_failed_product_and_keeps_success_cached():
    instance, provider = service()
    original = provider.get_work

    async def maybe_missing(product_id, **options):
        if product_id == "RJ99999999":
            raise ServiceError(ErrorCode.NOT_FOUND, "Missing.", details={"reason": "unavailable"})
        return await original(product_id, **options)

    provider.get_work = maybe_missing
    with pytest.raises(ServiceError) as caught:
        await instance.work_get(["RJ01655815", "RJ99999999"], "summary", "ja_JP", "ko_KR", 1200)
    assert caught.value.code is ErrorCode.NOT_FOUND
    assert caught.value.details == {"product_id": "RJ99999999", "reason": "unavailable"}
    retry = await instance.work_get("RJ01655815", "summary", "ja_JP", "ko_KR", 1200)
    assert retry["data"]["product_id"] == "RJ01655815"
    assert provider.work_calls == 1


@pytest.mark.asyncio
async def test_metadata_warnings_survive_cache_and_are_not_exposed_as_internal_fields():
    instance, provider = service()

    async def incomplete(product_id, **options):
        return {"product_id": product_id, "_warnings": ["Missing table."]}

    provider.get_work = incomplete
    for _ in range(2):
        result = await instance.work_get("RJ01655815", "details", "ko_KR", "ko_KR", 1200)
        assert "_warnings" not in result["data"]
        assert result["meta"]["warnings"] == ["RJ01655815: Missing table."]
        assert (
            "data.metadata_diagnostics.unrecognized_row_labels[]"
            in result["meta"]["untrusted_fields"]
        )


@pytest.mark.asyncio
async def test_search_reports_source_omissions_and_native_cross_storefront_results():
    instance, provider = service()

    async def native_results(query, **options):
        return (
            [
                {
                    "product_id": "RJ01655815",
                    "review_count": None,
                    "url": "https://www.dlsite.com/home/work/=/product_id/RJ01655815.html",
                }
            ],
            False,
        )

    provider.search_page = native_results
    result = await instance.search("東方", "maniax", "", 3, "ko_KR", "ko_KR")
    assert result["data"]["applied_filters"] == {"work_category": "doujin", "sex_category": "male"}
    assert result["data"]["result_sites"] == ["home"]
    assert any("null does not mean zero" in w for w in result["meta"]["warnings"])
    assert any("other storefront" in w for w in result["meta"]["warnings"])


@pytest.mark.asyncio
async def test_unfiltered_old_search_cursor_cannot_continue_filtered_results():
    instance, _ = service()
    old = instance.cursor.encode(
        scope="search",
        filters={"query": "東方", "site": "books", "locale": "ja_JP", "price_locale": "ko_KR"},
        state={"page": 1, "offset": 3},
    )
    with pytest.raises(ServiceError) as caught:
        await instance.search("東方", "books", old, 3, "ja_JP", "ko_KR")
    assert caught.value.code is ErrorCode.CURSOR_MISMATCH


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


@pytest.mark.asyncio
async def test_review_cursor_can_traverse_a_ten_thousand_item_corpus() -> None:
    instance, _ = service()
    cursor = ""
    seen: list[str] = []
    first: dict[str, object] | None = None
    for _ in range(500):
        page = await instance.work_get("RJ294126", "reviews", "ja_JP", "ko_KR", 100, cursor, 100)
        first = first or page
        page_ids = [str(item["review_id"]) for item in page["items"]]
        assert not set(page_ids).intersection(seen)
        seen.extend(page_ids)
        cursor = str(page["page"]["next_cursor"] or "")
        if not cursor:
            break
    assert first is not None
    assert first["data"]["total_reviews_snapshot"] == 10_000
    assert "items[].review" in first["meta"]["untrusted_fields"]
    assert seen == [str(index) for index in range(10_000)]
    assert page["data"]["complete"] is True


@pytest.mark.asyncio
async def test_review_cursor_rejects_changed_text_limit() -> None:
    instance, _ = service()
    first = await instance.work_get("RJ294126", "reviews", "ja_JP", "ko_KR", 1200, "", 5)
    with pytest.raises(ServiceError) as caught:
        await instance.work_get(
            "RJ294126",
            "reviews",
            "ja_JP",
            "ko_KR",
            1000,
            first["page"]["next_cursor"],
            5,
        )
    assert caught.value.code is ErrorCode.CURSOR_MISMATCH


@pytest.mark.asyncio
async def test_review_page_shortens_without_skipping_when_byte_budget_is_hit() -> None:
    instance, provider = service()

    async def large_page(product_id: str, **options: object) -> list[dict[str, object]]:
        page = int(options["page"])
        limit = int(options["limit"])
        start = (page - 1) * limit
        return [
            {
                "review_id": str(index),
                "reviewer_name": "reader",
                "title": "title",
                "review": "x" * 1200,
                "review_truncated": False,
            }
            for index in range(start, start + limit)
        ]

    provider.get_review_page = large_page  # type: ignore[method-assign]
    first = await instance.work_get("RJ294126", "reviews", "ja_JP", "ko_KR", 1200, "", 20)
    assert 1 <= first["page"]["returned"] < 20
    second = await instance.work_get(
        "RJ294126",
        "reviews",
        "ja_JP",
        "ko_KR",
        1200,
        first["page"]["next_cursor"],
        20,
    )
    assert second["items"][0]["review_id"] == str(first["page"]["returned"])
    assert any("byte budget" in warning for warning in first["meta"]["warnings"])
