"""Unauthenticated DLsite provider built on dlsite-async plus public search HTML."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup, Tag
from dlsite_async import DlsiteAPI
from dlsite_async.exceptions import DlsiteError

from .contracts import ErrorCode, ServiceError

WORK_ID_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{5,10})(?!\d)", re.IGNORECASE)
MAKER_ID_RE = re.compile(r"(?<![A-Z0-9])([RBV]G\d{4,10})(?!\d)", re.IGNORECASE)
SUPPORTED_SITES = {"maniax", "home", "books", "soft", "pro", "appx"}
SUPPORTED_LOCALES = {"ja_JP", "en_US", "ko_KR", "zh_CN", "zh_TW"}
LOCALE_CURRENCY = {
    "ja_JP": "JPY",
    "en_US": "USD",
    "ko_KR": "KRW",
    "zh_CN": "CNY",
    "zh_TW": "TWD",
}
USER_AGENT = "dlsite-mcp/1.1.1 (+https://github.com/BK927/dlsite-mcp)"


def normalize_work_id(reference: str) -> str:
    match = WORK_ID_RE.search(reference.strip())
    if not match:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "work must be a DLsite product ID or product URL.",
            details={"examples": ["RJ294126", "BJ370220"]},
        )
    return match.group(1).upper()


def normalize_maker_id(reference: str) -> str:
    match = MAKER_ID_RE.search(reference.strip())
    if not match:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "maker must be a DLsite maker ID or maker profile URL.",
            details={"examples": ["RG51931", "BG01675"]},
        )
    return match.group(1).upper()


def validate_locale(locale: str) -> str:
    if locale not in SUPPORTED_LOCALES:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "Unsupported locale.",
            details={"allowed": sorted(SUPPORTED_LOCALES)},
        )
    return locale


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    return value


def _int_text(node: Tag | None) -> int | None:
    if node is None:
        return None
    digits = re.sub(r"[^0-9]", "", node.get_text(" ", strip=True))
    return int(digits) if digits else None


def _parse_currency(node: Tag, currency: str) -> float | int | None:
    holder = node.select_one(".work_price [data-currency_price]")
    if holder is None:
        return None
    try:
        value = json.loads(holder.get("data-currency_price", "{}"))[currency]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return round(value) if currency in {"JPY", "KRW", "CNY", "TWD"} else round(value, 2)


def parse_search_html(
    html: str, *, site: str, price_locale: str
) -> tuple[list[dict[str, Any]], bool]:
    soup = BeautifulSoup(html, "html.parser")
    currency = LOCALE_CURRENCY[price_locale]
    items: list[dict[str, Any]] = []
    for node in soup.select("[data-list_item_product_id]"):
        if not isinstance(node, Tag):
            continue
        product_id = str(node.get("data-list_item_product_id", "")).upper()
        if not WORK_ID_RE.fullmatch(product_id):
            continue
        title_link = node.select_one(".work_name a")
        maker_link = node.select_one(".maker_name a[href*='maker_id']")
        category = node.select_one(".work_category a")
        image_node = node.select_one("thumb-with-ng-filter-block")
        image = None
        if image_node is not None:
            candidates = str(image_node.get(":thumb-candidates", ""))
            image_match = re.search(r"'(//[^']+)'", candidates)
            image = _absolute_url(image_match.group(1)) if image_match else None
        rating_node = node.select_one(".star_rating")
        review_node = node.select_one(".work_review a")
        discount_node = node.select_one(".work_deals .type_sale")
        discount = _int_text(discount_node)
        maker_url = maker_link.get("href") if maker_link else None
        maker_match = MAKER_ID_RE.search(str(maker_url or ""))
        items.append(
            {
                "product_id": product_id,
                "title": title_link.get_text(" ", strip=True) if title_link else None,
                "maker_id": maker_match.group(1).upper() if maker_match else None,
                "maker_name": maker_link.get_text(" ", strip=True) if maker_link else None,
                "category": category.get_text(" ", strip=True) if category else None,
                "price": {"amount": _parse_currency(node, currency), "currency": currency},
                "discount_percent": discount,
                "sales_count": _int_text(node.select_one(".work_dl span")),
                "rating_count": _int_text(rating_node),
                "review_count": _int_text(review_node),
                "image": image,
                "url": (
                    str(title_link.get("href"))
                    if title_link and title_link.get("href")
                    else f"https://www.dlsite.com/{site}/work/=/product_id/{product_id}.html"
                ),
            }
        )
    return items, bool(soup.select_one("a[href*='/page/2'], a.next"))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_review_payload(payload: dict[str, Any], *, max_chars: int) -> list[dict[str, Any]]:
    """Normalize DLsite's public review JSON without retaining undocumented fields."""
    items: list[dict[str, Any]] = []
    for raw in payload.get("review_list") or []:
        if not isinstance(raw, dict) or not raw.get("member_review_id"):
            continue
        title = str(raw.get("review_title") or "")
        text = str(raw.get("review_text") or "")
        translated_locale = None
        for translation in raw.get("translations") or []:
            if not isinstance(translation, dict):
                continue
            translated_title = translation.get("title")
            translated_text = translation.get("text")
            if translated_title or translated_text:
                title = str(translated_title or title)
                text = str(translated_text or text)
                translated_locale = translation.get("locale")
                break
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + "…"
        genres = raw.get("genre") or {}
        items.append(
            {
                "review_id": str(raw["member_review_id"]),
                "reviewer_id": str(raw.get("reviewer_id") or "") or None,
                "reviewer_name": str(raw.get("nick_name") or "") or None,
                "title": title or None,
                "review": text,
                "rating": _optional_int(raw.get("rate")),
                "recommended": str(raw.get("recommend") or "0") == "1",
                "spoiler": str(raw.get("spoiler") or "0") == "1",
                "purchased": str(raw.get("is_purchased") or "0") == "1",
                "posted_at": raw.get("entry_date"),
                "published_at": raw.get("regist_date"),
                "helpful_count": _optional_int(raw.get("good_review")),
                "unhelpful_count": _optional_int(raw.get("bad_review")),
                "reviewer_rank": str(raw.get("reviewer_rank") or "") or None,
                "genres": list(genres.values()) if isinstance(genres, dict) else [],
                "original_locale": raw.get("original_lang"),
                "translated_locale": translated_locale,
                "review_truncated": truncated,
            }
        )
    return items


class DlsiteProvider:
    def __init__(self, *, timeout_seconds: float = 25.0) -> None:
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _prepare(api: DlsiteAPI) -> None:
        api.session.headers.update({"User-Agent": USER_AGENT})

    async def _run(self, operation: Any) -> Any:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await operation
        except ServiceError:
            raise
        except TimeoutError as exc:
            raise ServiceError(
                ErrorCode.TIMEOUT, "DLsite did not respond in time.", retryable=True
            ) from exc
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                raise ServiceError(ErrorCode.NOT_FOUND, "DLsite resource was not found.") from exc
            if exc.status == 429:
                raise ServiceError(
                    ErrorCode.RATE_LIMITED, "DLsite rate limited the request.", retryable=True
                ) from exc
            raise ServiceError(
                ErrorCode.UPSTREAM_ERROR,
                f"DLsite returned HTTP {exc.status}.",
                retryable=exc.status >= 500,
            ) from exc
        except DlsiteError as exc:
            raise ServiceError(ErrorCode.NOT_FOUND, "DLsite resource was not found.") from exc
        except aiohttp.ClientError as exc:
            raise ServiceError(
                ErrorCode.UPSTREAM_ERROR, "DLsite could not complete the request.", retryable=True
            ) from exc

    async def get_work(
        self,
        product_id: str,
        *,
        view: str,
        locale: str,
        price_locale: str,
        max_chars: int,
    ) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            async with DlsiteAPI(locale=locale) as api:
                self._prepare(api)
                work = await (
                    api.get_work(product_id) if view == "details" else api.product_info(product_id)
                )
                url = "https://www.dlsite.com/maniax/product/info/ajax"
                async with api.get(url, params={"product_id": product_id}) as response:
                    raw = (await response.json()).get(product_id, {})
            data = _jsonable(work)
            if data.get("description") and len(data["description"]) > max_chars:
                data["description"] = data["description"][:max_chars] + "…"
                data["description_truncated"] = True
            data["work_image"] = _absolute_url(data.get("work_image"))
            data["sample_images"] = [
                _absolute_url(item) for item in data.get("sample_images") or []
            ]
            currency = LOCALE_CURRENCY[price_locale]
            price = raw.get("currency_price", {}).get(currency)
            official = raw.get("currency_official_price", {}).get(currency)
            integer_currency = currency in {"JPY", "KRW", "CNY", "TWD"}
            data["commerce"] = {
                "price": round(price)
                if integer_currency and price is not None
                else round(price, 2)
                if price is not None
                else None,
                "official_price": round(official)
                if integer_currency and official is not None
                else round(official, 2)
                if official is not None
                else None,
                "currency": currency,
                "discount_percent": raw.get("discount_rate"),
                "discount_end": raw.get("discount_end_date"),
                "is_sale": bool(raw.get("is_sale")),
                "is_sold_out": bool(raw.get("is_sold_out")),
            }
            data["public_metrics"] = {
                "sales_count": raw.get("dl_count"),
                "sales_count_all_editions": raw.get("dl_count_total"),
                "wishlist_count": raw.get("wishlist_count"),
                "rating_average": raw.get("rate_average_2dp"),
                "rating_count": raw.get("rate_count"),
                "rating_distribution": raw.get("rate_count_detail") or [],
                "review_count": raw.get("review_count"),
            }
            data["url"] = (
                f"https://www.dlsite.com/{data.get('site_id', 'maniax')}/work/=/product_id/{product_id}.html"
            )
            return data

        return await self._run(load())

    async def get_maker(self, maker_id: str, *, locale: str) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            async with DlsiteAPI(locale=locale) as api:
                self._prepare(api)
                maker = await api.get_circle(maker_id)
            data = _jsonable(maker)
            data["maker_type"] = maker.maker_type.value
            data["url"] = f"https://www.dlsite.com/maniax/circle/profile/=/maker_id/{maker_id}.html"
            return data

        return await self._run(load())

    async def search_page(
        self,
        query: str,
        *,
        site: str,
        page: int,
        locale: str,
        price_locale: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        async def load() -> tuple[list[dict[str, Any]], bool]:
            encoded = quote(query, safe="")
            base = f"https://www.dlsite.com/{site}/fsr/=/keyword/{encoded}/per_page/30"
            # DLsite returns 404 for an explicit `/page/1` even though later
            # page numbers use that segment.
            url = base if page == 1 else f"{base}/page/{page}"
            async with DlsiteAPI(locale=locale) as api:
                self._prepare(api)
                async with api.get(url) as response:
                    html = await response.text()
            items, _ = parse_search_html(html, site=site, price_locale=price_locale)
            if not items:
                return [], False
            # DLsite renders up to 30 records per page. A full page can continue.
            return items, len(items) >= 30

        return await self._run(load())

    async def get_review_overview(self, product_id: str, *, locale: str) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            params = {
                "product_id": product_id,
                "limit": 1,
                "mix_pickup": "true",
                "locale": locale,
            }
            async with DlsiteAPI(locale=locale) as api:
                self._prepare(api)
                async with api.get(
                    "https://www.dlsite.com/maniax/api/review", params=params
                ) as response:
                    payload = await response.json()
            if not isinstance(payload, dict) or not payload.get("is_success"):
                raise ServiceError(
                    ErrorCode.UPSTREAM_ERROR,
                    "DLsite could not return the public review index.",
                    retryable=True,
                )
            return {
                "product_id": product_id,
                "product_name": payload.get("product_name"),
                "total_reviews": max(0, _optional_int(payload.get("count")) or 0),
                "reviews_available": not bool(payload.get("review_deny")),
            }

        return await self._run(load())

    async def get_review_page(
        self,
        product_id: str,
        *,
        page: int,
        limit: int,
        locale: str,
        max_chars: int,
    ) -> list[dict[str, Any]]:
        async def load() -> list[dict[str, Any]]:
            params = {
                "product_id": product_id,
                "limit": limit,
                "page": page,
                "order": "regist_d",
                "locale": locale,
            }
            async with DlsiteAPI(locale=locale) as api:
                self._prepare(api)
                async with api.get(
                    "https://www.dlsite.com/maniax/api/review", params=params
                ) as response:
                    payload = await response.json()
            if not isinstance(payload, dict) or not payload.get("is_success"):
                raise ServiceError(
                    ErrorCode.UPSTREAM_ERROR,
                    "DLsite could not return the public review page.",
                    retryable=True,
                )
            return parse_review_payload(payload, max_chars=max_chars)

        return await self._run(load())
