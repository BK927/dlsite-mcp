"""Bounded public service layer."""

from __future__ import annotations

import asyncio
from typing import Any

from .cache import TtlLruCache
from .contracts import ErrorCode, ServiceError, collection_envelope, entity_envelope
from .cursor import CursorCodec
from .provider import (
    SUPPORTED_SITES,
    DlsiteProvider,
    normalize_maker_id,
    normalize_work_id,
    validate_locale,
)


class DlsiteService:
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
    ) -> dict[str, Any]:
        validate_locale(locale)
        validate_locale(price_locale)
        references = [work] if isinstance(work, str) else work
        if not references or len(references) > 20:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "work must contain 1 to 20 references.")
        if view == "details" and len(references) > 5:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "details view accepts at most 5 works.")
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
                return await one(product_id)

        items = list(await asyncio.gather(*(bounded(product_id) for product_id in ids)))
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
            )
        return collection_envelope(
            items,
            data={"view": view},
            untrusted_fields=[field.replace("data.", "items[].") for field in untrusted],
        )

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
        filters = {"query": query, "site": site, "locale": locale, "price_locale": price_locale}
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
        return collection_envelope(
            items,
            next_cursor=next_cursor,
            data={"query": query, "site": site},
            provider="dlsite-public-search",
            untrusted_fields=["items[].title", "items[].maker_name", "items[].category"],
        )
