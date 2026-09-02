from __future__ import annotations

import pytest

from dlsite_mcp.contracts import ErrorCode, ServiceError
from dlsite_mcp.provider import (
    normalize_maker_id,
    normalize_work_id,
    parse_review_payload,
    parse_search_html,
    validate_locale,
)

SEARCH_HTML = """
<html><body>
<li class="search_result_img_box_inner" data-list_item_product_id="RJ123456">
  <dl class="work_img_main">
    <dt><thumb-with-ng-filter-block
      :thumb-candidates="['//img.dlsite.jp/RJ123456.webp']"></thumb-with-ng-filter-block></dt>
    <dd class="work_category_free_sample"><div class="work_category"><a>Voice / ASMR</a></div></dd>
    <dd class="work_name"><a href="https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html">Untrusted title</a></dd>
    <dd class="maker_name"><a href="https://www.dlsite.com/maniax/circle/profile/=/maker_id/RG12345.html">Untrusted maker</a></dd>
    <dd class="work_price_wrap"><span class="work_price"><div
      data-currency_price='{"JPY":1100,"KRW":10234.6}'></div></span></dd>
    <dd class="work_dl"><span>1,234</span></dd>
    <dd class="work_rating"><div class="star_rating">(56)</div><div class="work_review"><a>(7)</a></div></dd>
    <dd class="work_deals"><span class="type_sale">30%OFF</span></dd>
  </dl>
</li>
<a href="/maniax/fsr/=/keyword/test/page/2">next</a>
</body></html>
"""


def test_reference_normalization() -> None:
    assert (
        normalize_work_id("https://www.dlsite.com/maniax/work/=/product_id/rj294126.html")
        == "RJ294126"
    )
    assert normalize_maker_id("profile/=/maker_id/rg51931.html") == "RG51931"


@pytest.mark.parametrize("value", ["", "not-an-id", "123456"])
def test_invalid_work_reference(value: str) -> None:
    with pytest.raises(ServiceError) as caught:
        normalize_work_id(value)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_locale_is_strict() -> None:
    assert validate_locale("ko_KR") == "ko_KR"
    with pytest.raises(ServiceError) as caught:
        validate_locale("ko-KR")
    assert caught.value.details["allowed"]


def test_search_parser_normalizes_public_fields() -> None:
    items, has_more = parse_search_html(SEARCH_HTML, site="maniax", price_locale="ko_KR")
    assert has_more is True
    assert items == [
        {
            "product_id": "RJ123456",
            "title": "Untrusted title",
            "maker_id": "RG12345",
            "maker_name": "Untrusted maker",
            "category": "Voice / ASMR",
            "price": {"amount": 10235, "currency": "KRW"},
            "discount_percent": 30,
            "sales_count": 1234,
            "rating_count": 56,
            "review_count": 7,
            "image": "https://img.dlsite.jp/RJ123456.webp",
            "url": "https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html",
        }
    ]


def test_review_parser_normalizes_and_bounds_untrusted_text() -> None:
    payload = {
        "review_list": [
            {
                "member_review_id": "123",
                "reviewer_id": "REV001",
                "nick_name": "reader",
                "review_title": "title",
                "review_text": "abcdef",
                "rate": "5",
                "recommend": "1",
                "spoiler": "1",
                "is_purchased": "1",
                "good_review": "7",
                "bad_review": "2",
                "entry_date": "2026-01-01 00:00:00",
                "regist_date": "2026-01-02 00:00:00",
                "genre": {"1": "ASMR"},
                "original_lang": "ja_JP",
                "translations": [{"locale": "ko_KR", "title": "번역", "text": "가나다라마바사"}],
            }
        ]
    }
    assert parse_review_payload(payload, max_chars=5) == [
        {
            "review_id": "123",
            "reviewer_id": "REV001",
            "reviewer_name": "reader",
            "title": "번역",
            "review": "가나다라마…",
            "rating": 5,
            "recommended": True,
            "spoiler": True,
            "purchased": True,
            "posted_at": "2026-01-01 00:00:00",
            "published_at": "2026-01-02 00:00:00",
            "helpful_count": 7,
            "unhelpful_count": 2,
            "reviewer_rank": None,
            "genres": ["ASMR"],
            "original_locale": "ja_JP",
            "translated_locale": "ko_KR",
            "review_truncated": True,
        }
    ]
