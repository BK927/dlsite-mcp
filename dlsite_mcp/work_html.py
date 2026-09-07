"""Compatibility adapter for localized public work metadata in dlsite-async 0.10.2."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup
from dlsite_async._scraper import parse_work_html

# Canonical headings understood by the pinned upstream parser. Values remain in
# the requested language; only labels are normalized before parsing.
ROWS = {
    "Circle": ("circle", "サークル名", "서클명", "社团名", "社團名"),
    "Brand": ("brand", "ブランド名", "브랜드명", "品牌名", "品牌名稱"),
    "Publisher": ("publisher", "出版社名", "출판사명", "出版社名称", "出版社名稱"),
    "Label": ("label", "レーベル", "레이블", "厂牌", "廠牌"),
    "Update information": (
        "modified_date",
        "更新情報",
        "最終更新日",
        "Last updated",
        "갱신 정보",
        "更新信息",
        "更新資訊",
    ),
    "Published date": ("announce_date", "予告開始日", "예고 개시일", "预告开始日", "預告開始日"),
    "Page count": ("page_count", "ページ数", "페이지 수", "页数", "頁數"),
    "File format": ("file_format", "ファイル形式", "파일 형식", "文件形式", "檔案形式"),
    "File size": ("file_size", "ファイル容量", "파일 용량", "文件容量", "檔案容量"),
    "Genre": ("genre", "ジャンル", "장르", "分类", "分類"),
    "Author": ("author", "作者", "著者", "저자", "작가"),
    "Event": ("event", "イベント", "이벤트", "活动", "活動"),
    "Illustration": ("illustration", "イラスト", "일러스트", "插画", "插畫"),
    "Music": ("music", "音楽", "음악", "音乐", "音樂"),
    "Scenario": ("scenario", "シナリオ", "시나리오", "剧本", "劇本"),
    "Voice Actor": ("voice_actor", "声優", "성우", "声优", "聲優"),
    "Writer": ("writer", "作家"),
    "Series": (
        "title_name_masked",
        "シリーズ",
        "シリーズ名",
        "Series name",
        "시리즈",
        "시리즈명",
        "系列",
        "系列名",
    ),
    "Supported languages": ("language", "対応言語", "대응 언어", "支持的语言", "對應語言"),
}
HEADINGS = {
    alias: (canonical, values[0])
    for canonical, values in ROWS.items()
    for alias in (canonical, *values[1:])
}
# These rows are supplied by product/info JSON, not the HTML detail adapter.
JSON_HEADINGS = {
    "販売日",
    "Release date",
    "판매일",
    "发售日",
    "販賣日",
    "年齢指定",
    "Age",
    "연령 지정",
    "年龄指定",
    "年齡指定",
    "作品形式",
    "Product format",
    "작품 형식",
}
# Storefronts use different quotation marks. Match the complete shop footer,
# including its download-shop wording, so ordinary DLsite mentions survive.
KOREAN_PROMO = re.compile(
    r"""(?P<quote>["'])DLsite[^"'\n]*(?P=quote)(?:은|는)\s.*다운로드\s*(?:숍|샵).*(?P=quote)DLsite(?P=quote)!\s*$""",
    re.DOTALL,
)
MAX_DIAGNOSTIC_LABELS = 8
MAX_DIAGNOSTIC_LABEL_CHARS = 80
MONTHS = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}


def _date(text: str) -> datetime | None:
    match = re.search(r"(\d{4})\s*[年년/-]\s*(\d{1,2})\s*[月월/-]\s*(\d{1,2})", text)
    if match:
        parts = tuple(map(int, match.groups()))
    else:
        match = re.search(r"([A-Za-z]+)[/\s]+(\d{1,2})[/\s,]+(\d{4})", text)
        if not match or match[1][:3] not in MONTHS:
            return None
        parts = (int(match[3]), MONTHS[match[1][:3]], int(match[2]))
    try:
        return datetime(*parts)
    except ValueError:
        return None


def parse_work_details(html: str) -> tuple[dict[str, Any], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    headers = soup.select("#work_maker th, #work_outline th, dl.c-productInfo__box dt")
    expected = set()
    overrides: dict[str, Any] = {}
    unknown_labels: list[str] = []
    for header in headers:
        label = unicodedata.normalize("NFKC", header.get_text(" ", strip=True))
        mapping = HEADINGS.get(label)
        if not mapping:
            if label not in JSON_HEADINGS:
                unknown_labels.append(" ".join(label.split()))
            continue
        canonical, field = mapping
        expected.add(field)
        value = header.find_next_sibling("td" if header.name == "th" else "dd")
        if value is not None:
            if field in {"modified_date", "announce_date"}:
                parsed_date = _date(value.get_text(" ", strip=True))
                if parsed_date:
                    overrides[field] = parsed_date
                    # Avoid the upstream date parser's locale-dependent formats.
                    value.string = parsed_date.strftime("%Y年%m月%d日")
            elif field == "language":
                overrides[field] = value.get_text(" ", strip=True) or None
        header.string = canonical
    data = parse_work_html(str(soup))
    data.update(overrides)
    if data.get("description"):
        data["description"] = KOREAN_PROMO.sub("", data["description"]).strip() or None
        data["description_source"] = "meta_description"
    warnings = []
    missing = sorted(field for field in expected if data.get(field) in (None, "", []))
    if missing:
        warnings.append("Work metadata could not be parsed: " + ", ".join(missing) + ".")
    if unknown_labels:
        warnings.append(
            "Some work metadata rows were not recognized by the parser; "
            "see metadata_diagnostics.unrecognized_row_labels (untrusted source text)."
        )
    if unknown_labels or missing:
        unique_labels = list(dict.fromkeys(unknown_labels))
        data["metadata_diagnostics"] = {
            "unrecognized_row_labels": [
                label[:MAX_DIAGNOSTIC_LABEL_CHARS]
                for label in unique_labels[:MAX_DIAGNOSTIC_LABELS]
            ],
            "unrecognized_row_count": len(unknown_labels),
            "labels_truncated": len(unique_labels) > MAX_DIAGNOSTIC_LABELS
            or any(len(label) > MAX_DIAGNOSTIC_LABEL_CHARS for label in unique_labels),
            "failed_fields": missing,
        }
    if not headers:
        warnings.append("The source page did not expose a recognized work metadata table.")
    return data, warnings
