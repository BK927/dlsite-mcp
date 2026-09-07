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


def test_missing_metadata_table_is_not_silently_complete():
    _, warnings = parse_work_details("<html><title>Unexpected page</title></html>")
    assert any("metadata table" in warning for warning in warnings)


def test_description_mention_of_dlsite_is_preserved():
    data, _ = parse_work_details(
        '<meta name="description" content="DLsite 전용 게임. &quot;DLsite&quot; 안내 포함.">'
    )
    assert data["description"] == 'DLsite 전용 게임. "DLsite" 안내 포함.'
