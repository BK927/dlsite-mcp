"""Bounded public service layer."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .cache import TtlLruCache
from .contracts import (
    ErrorCode,
    ServiceError,
    collection_envelope,
    compact_size,
    entity_envelope,
)
from .cursor import CursorCodec
from .provider import (
    SEARCH_FILTERS,
    SUPPORTED_SITES,
    DlsiteProvider,
    normalize_maker_id,
    normalize_work_id,
    validate_locale,
)


class DlsiteService:
    REVIEW_PROVIDER_PAGE_SIZE = 30
    REVIEW_ENVELOPE_BUDGET = 11 * 1024

    def __init__(self, provider: DlsiteProvider, cache: TtlLruCache, cursor: CursorCodec) -> None:
        self.provider = provider
        self.cache = cache
        self.cursor = cursor

    async def work_get(
        self,
        work: str | list[str],
        view: str,
        locale: str,
        price_locale: str,
        max_chars: int,
        cursor: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        validate_locale(locale)
        validate_locale(price_locale)
        references = [work] if isinstance(work, str) else work
        if not references or len(references) > 20:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "work must contain 1 to 20 references.")
        if view == "details" and len(references) > 5:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "details view accepts at most 5 works.")
        if view == "reviews":
            if not isinstance(work, str):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT, "reviews view accepts exactly one work."
                )
            return await self.reviews_get(work, locale, cursor, limit, max_chars)
        if cursor:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT, "cursor is only supported by the reviews view."
            )
        ids = [normalize_work_id(str(reference)) for reference in references]
        if len(set(ids)) != len(ids):
            ids = list(dict.fromkeys(ids))

        async def one(product_id: str) -> dict[str, Any]:
            key = f"work:{product_id}:{view}:{locale}:{price_locale}:{max_chars}"
            return await self.cache.get_or_load(
                key,
                lambda: self.provider.get_work(
                    product_id,
                    view=view,
                    locale=locale,
                    price_locale=price_locale,
                    max_chars=max_chars,
                ),
            )

        semaphore = asyncio.Semaphore(4)

        async def bounded(product_id: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await one(product_id)
                except ServiceError as exc:
                    raise ServiceError(
                        exc.code,
                        exc.message,
                        retryable=exc.retryable,
                        schema_uri=exc.schema_uri,
                        details={**exc.details, "product_id": product_id},
                    ) from exc

        # Copy cached records before moving provider diagnostics into the envelope.
        items = [dict(item) for item in await asyncio.gather(*(bounded(pid) for pid in ids))]
        warnings = []
        for product_id, item in zip(ids, items, strict=True):
            warnings.extend(f"{product_id}: {warning}" for warning in item.pop("_warnings", []))
        untrusted = [
            "data.work_name",
            "data.work_name_masked",
            "data.title_name",
            "data.title_name_masked",
            "data.circle",
            "data.brand",
            "data.publisher",
            "data.description",
            "data.scenario[]",
            "data.illustration[]",
            "data.voice_actor[]",
            "data.author[]",
            "data.music[]",
            "data.writer[]",
            "data.genre[]",
            "data.language",
            "data.metadata_diagnostics.unrecognized_row_labels[]",
            "data.label",
            "data.event[]",
            "data.work_image",
            "data.sample_images[]",
        ]
        if isinstance(work, str):
            product_id = ids[0]
            return entity_envelope(
                items[0],
                canonical_uri=f"dlsite://entity/work/{product_id}",
                untrusted_fields=untrusted,
                warnings=warnings,
            )
        return collection_envelope(
            items,
            data={"view": view},
            warnings=warnings,
            untrusted_fields=[field.replace("data.", "items[].") for field in untrusted],
        )

    async def _review_page(
        self, product_id: str, page: int, locale: str, max_chars: int
    ) -> list[dict[str, Any]]:
        return await self.cache.get_or_load(
            f"reviews:{product_id}:{page}:{locale}:{max_chars}",
            lambda: self.provider.get_review_page(
                product_id,
                page=page,
                limit=self.REVIEW_PROVIDER_PAGE_SIZE,
                locale=locale,
                max_chars=max_chars,
            ),
            ttl_seconds=300,
        )

    async def _locate_review_anchor(
        self,
        product_id: str,
        anchor: str,
        consumed: int,
        locale: str,
        max_chars: int,
    ) -> int:
        nominal_page = (max(0, consumed - 1) // self.REVIEW_PROVIDER_PAGE_SIZE) + 1
        pages = [nominal_page]
        for drift in range(1, 4):
            if nominal_page - drift >= 1:
                pages.append(nominal_page - drift)
            pages.append(nominal_page + drift)
        for page in pages:
            items = await self._review_page(product_id, page, locale, max_chars)
            for index, item in enumerate(items):
                if item.get("review_id") == anchor:
                    return (page - 1) * self.REVIEW_PROVIDER_PAGE_SIZE + index + 1
        raise ServiceError(
            ErrorCode.CURSOR_MISMATCH,
            "The public review list changed too much to continue safely; restart without a cursor.",
            retryable=True,
            details={"restart_required": True},
        )

    async def _collect_reviews(
        self,
        product_id: str,
        start: int,
        wanted: int,
        locale: str,
        max_chars: int,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        position = start
        while len(output) < wanted:
            page = position // self.REVIEW_PROVIDER_PAGE_SIZE + 1
            offset = position % self.REVIEW_PROVIDER_PAGE_SIZE
            source = await self._review_page(product_id, page, locale, max_chars)
            if offset >= len(source):
                break
            take = source[offset : offset + wanted - len(output)]
            output.extend(take)
            position += len(take)
            if len(source) < self.REVIEW_PROVIDER_PAGE_SIZE:
                break
        return output

    async def reviews_get(
        self,
        work: str,
        locale: str,
        cursor: str,
        limit: int,
        max_chars: int,
    ) -> dict[str, Any]:
        product_id = normalize_work_id(work)
        if not 1 <= limit <= 100:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "limit must be between 1 and 100.")
        if not 100 <= max_chars <= 4_000:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "reviews view max_chars must be between 100 and 4000.",
            )
        filters = {"product_id": product_id, "locale": locale, "max_chars": max_chars}
        if cursor:
            state = self.cursor.decode(cursor, scope="reviews", filters=filters)
            consumed = int(state.get("consumed", -1))
            snapshot_total = int(state.get("snapshot_total", -1))
            anchor = str(state.get("anchor") or "")
            product_name = state.get("product_name")
            if consumed < 1 or snapshot_total < consumed or not anchor:
                raise ServiceError(ErrorCode.CURSOR_MISMATCH, "The review cursor state is invalid.")
            start = await self._locate_review_anchor(
                product_id, anchor, consumed, locale, max_chars
            )
        else:
            overview = await self.cache.get_or_load(
                f"review-overview:{product_id}:{locale}",
                lambda: self.provider.get_review_overview(product_id, locale=locale),
                ttl_seconds=60,
            )
            if not overview["reviews_available"]:
                raise ServiceError(
                    ErrorCode.NOT_FOUND, "Public reviews are unavailable for this work."
                )
            consumed = 0
            snapshot_total = int(overview["total_reviews"])
            product_name = overview.get("product_name")
            start = 0

        wanted = min(limit, max(0, snapshot_total - consumed))
        candidates = await self._collect_reviews(product_id, start, wanted, locale, max_chars)
        warnings: list[str] = []
        if wanted and not candidates:
            warnings.append("The mutable public review list ended before its initial count.")

        def build(count: int) -> dict[str, Any]:
            selected = candidates[:count]
            new_consumed = consumed + len(selected)
            has_more = bool(selected) and new_consumed < snapshot_total
            next_cursor = None
            if has_more:
                next_cursor = self.cursor.encode(
                    scope="reviews",
                    filters=filters,
                    state={
                        "consumed": new_consumed,
                        "snapshot_total": snapshot_total,
                        "anchor": selected[-1]["review_id"],
                        "product_name": product_name,
                    },
                )
            return collection_envelope(
                selected,
                next_cursor=next_cursor,
                data={
                    "product_id": product_id,
                    "product_name": product_name,
                    "total_reviews_snapshot": snapshot_total,
                    "position_start": consumed,
                    "position_end": new_consumed,
                    "complete": new_consumed >= snapshot_total,
                    "order": "newest",
                    "max_chars_per_review": max_chars,
                },
                canonical_uri=f"dlsite://entity/work/{product_id}",
                provider="dlsite-public-reviews",
                warnings=warnings,
                untrusted_fields=[
                    "items[].reviewer_name",
                    "items[].title",
                    "items[].review",
                ],
            )

        count = len(candidates)
        envelope = build(count)
        while count > 1 and compact_size(envelope) > self.REVIEW_ENVELOPE_BUDGET:
            count -= 1
            envelope = build(count)
        if count < len(candidates):
            envelope["meta"]["warnings"].append(
                "Page shortened to preserve complete review records within the MCP byte budget."
            )
        return envelope

    async def maker_get(self, maker: str, locale: str) -> dict[str, Any]:
        validate_locale(locale)
        maker_id = normalize_maker_id(maker)
        data = await self.cache.get_or_load(
            f"maker:{maker_id}:{locale}",
            lambda: self.provider.get_maker(maker_id, locale=locale),
        )
        return entity_envelope(
            data,
            canonical_uri=f"dlsite://entity/maker/{maker_id}",
            untrusted_fields=["data.maker_name"],
        )

    async def search(
        self,
        query: str,
        site: str,
        cursor: str,
        limit: int,
        locale: str,
        price_locale: str,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query or len(query) > 200:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT, "query must contain 1 to 200 characters."
            )
        if site not in SUPPORTED_SITES:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                "Unsupported DLsite section.",
                details={"allowed": sorted(SUPPORTED_SITES)},
            )
        validate_locale(locale)
        validate_locale(price_locale)
        # Old cursors refer to unfiltered searches and must not continue a new scope.
        filters = {
            "query": query,
            "site": site,
            "locale": locale,
            "price_locale": price_locale,
            "scope_version": 2,
        }
        state = (
            self.cursor.decode(cursor, scope="search", filters=filters)
            if cursor
            else {"page": 1, "offset": 0}
        )
        page = int(state.get("page", 1))
        offset = int(state.get("offset", 0))
        if page < 1 or not 0 <= offset < 30:
            raise ServiceError(
                ErrorCode.CURSOR_MISMATCH, "The cursor state is outside the valid range."
            )
        all_items, page_has_more = await self.cache.get_or_load(
            f"search:{query}:{site}:{page}:{locale}:{price_locale}",
            lambda: self.provider.search_page(
                query,
                site=site,
                page=page,
                locale=locale,
                price_locale=price_locale,
            ),
            ttl_seconds=300,
        )
        items = all_items[offset : offset + limit]
        end = offset + len(items)
        next_state: dict[str, int] | None = None
        if end < len(all_items):
            next_state = {"page": page, "offset": end}
        elif page_has_more:
            next_state = {"page": page + 1, "offset": 0}
        next_cursor = (
            self.cursor.encode(scope="search", filters=filters, state=next_state)
            if next_state
            else None
        )
        result_sites = sorted(
            {
                parts[1]
                for item in items
                if len(parts := urlsplit(item.get("url") or "").path.split("/")) > 1 and parts[1]
            }
        )
        warnings = []
        if any(item.get("review_count") is None for item in items):
            warnings.append(
                "The search source did not expose a review count for some items; null does not mean zero."
            )
        if any(result_site != site for result_site in result_sites):
            warnings.append(
                "DLsite's section filters returned works with other storefront URLs; see result_sites and applied_filters."
            )
        return collection_envelope(
            items,
            next_cursor=next_cursor,
            data={
                "query": query,
                "site": site,
                "applied_filters": dict(SEARCH_FILTERS[site]),
                "result_sites": result_sites,
            },
            provider="dlsite-public-search",
            warnings=warnings,
            untrusted_fields=["items[].title", "items[].maker_name", "items[].category"],
        )
