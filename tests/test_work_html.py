from pathlib import Path

import pytest

from dlsite_mcp.work_html import parse_work_details

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("locale", "genre", "file_format"),
    [
        ("ja_JP", "東方Project", "アプリケーション"),
        ("en_US", "Touhou Project", "Application"),
        ("ko_KR", "동방Project", "어플리케이션"),
        ("zh_CN", "东方Project", "软件"),
        ("zh_TW", "東方Project", "應用程式"),
    ],
)
def test_live_metadata_excerpts_preserve_localized_values(locale, genre, file_format):
    data, warnings = parse_work_details(
        (FIXTURES / f"work_{locale}.html").read_text(encoding="utf-8")
    )
    assert data["circle"] == "ISY"
    assert data["modified_date"].isoformat() == "2026-07-08T00:00:00"
    assert data["genre"] == [genre]
    assert data["file_format"] == [file_format]
    assert data["file_size"] == "70.73MB"
    assert data["description"] == "健康診断の結果を書き換えて遊ぶ重病告知バラエティ(?)です。"
    assert data["description_source"] == "meta_description"
    assert warnings == []


def test_failed_date_and_unknown_heading_report_incomplete_metadata():
    html = (FIXTURES / "work_ko_KR.html").read_text(encoding="utf-8")
    html = html.replace("2026년 07월 08일", "2026년 99월 99일").replace("파일 용량", "new label")
    data, warnings = parse_work_details(html)
    assert data["circle"] == "ISY"
    assert "modified_date" not in data
    assert any("modified_date" in warning for warning in warnings)
    assert any("not recognized" in warning for warning in warnings)
    assert data["metadata_diagnostics"]["failed_fields"] == ["modified_date"]
    assert data["metadata_diagnostics"]["unrecognized_row_labels"] == ["new label"]


def test_missing_metadata_table_is_not_silently_complete():
    _, warnings = parse_work_details("<html><title>Unexpected page</title></html>")
    assert any("metadata table" in warning for warning in warnings)


def test_description_mention_of_dlsite_is_preserved():
    data, _ = parse_work_details(
        '<meta name="description" content="DLsite 전용 게임. &quot;DLsite&quot; 안내 포함.">'
    )
    assert data["description"] == 'DLsite 전용 게임. "DLsite" 안내 포함.'


@pytest.mark.parametrize(
    ("locale", "unknown_heading"),
    [
        ("ja_JP", "その他"),
        ("en_US", "Miscellaneous"),
        ("ko_KR", "기타"),
        ("zh_CN", "其他"),
        ("zh_TW", "其他"),
    ],
)
def test_book_storefront_footer_and_metadata_diagnostics(locale, unknown_heading):
    data, warnings = parse_work_details(
        (FIXTURES / f"book_{locale}.html").read_text(encoding="utf-8")
    )
    assert data["description"] == "立羽氏の魅力が詰まった珠玉の1冊!"
    assert data["title_name_masked"] == "立羽画集 Marvelous Grace"
    assert data["page_count"] == 135
    assert data["metadata_diagnostics"] == {
        "unrecognized_row_labels": [unknown_heading],
        "unrecognized_row_count": 1,
        "labels_truncated": False,
        "failed_fields": [],
    }
    assert any("metadata_diagnostics.unrecognized_row_labels" in warning for warning in warnings)


def test_commercial_game_footer_is_still_removed():
    html = (FIXTURES / "commercial_ko_KR.html").read_text(encoding="utf-8")
    data, warnings = parse_work_details(html)
    assert data["description"].endswith("全年齢版が登場!!")
    assert "DLsite" not in data["description"]
    assert data["brand"] == "ninetail/dualtail"
    assert warnings == []


@pytest.mark.parametrize(
    "description",
    [
        "'DLsite'은 게임에 등장하는 이름. 'DLsite'!",
        "'DLsite'은 다운로드 숍이라는 설명이 있는 작품.",
        "작품 소개 'DLsite'은 다운로드 숍. \"DLsite\"!",  # mismatched footer quotes
    ],
)
def test_incomplete_footer_or_ordinary_site_mention_is_preserved(description):
    from html import escape

    data, _ = parse_work_details(
        f'<meta name="description" content="{escape(description, quote=True)}">'
    )
    assert data["description"] == description


def test_unrecognized_labels_are_bounded_without_echoing_row_values():
    rows = "".join(
        f"<tr><th>{index}{'x' * 120}</th><td>private row value</td></tr>" for index in range(12)
    )
    data, warnings = parse_work_details(f'<table id="work_outline">{rows}</table>')
    diagnostic = data["metadata_diagnostics"]
    assert diagnostic["unrecognized_row_count"] == 12
    assert len(diagnostic["unrecognized_row_labels"]) == 8
    assert all(len(label) == 80 for label in diagnostic["unrecognized_row_labels"])
    assert diagnostic["labels_truncated"] is True
    assert "private row value" not in str(data)
    assert not any("x" * 80 in warning for warning in warnings)
