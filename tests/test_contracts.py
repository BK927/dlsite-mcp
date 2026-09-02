from __future__ import annotations

from dlsite_mcp.contracts import (
    ErrorCode,
    ServiceError,
    collection_envelope,
    compact_size,
    entity_envelope,
    error_result,
    success_result,
)


def test_success_has_structured_data_and_one_line_content() -> None:
    result = success_result(entity_envelope({"description": "provider data"}), "Work returned.")
    assert result.structured_content["data"]["description"] == "provider data"
    assert result.content[0].text == "Work returned."
    assert "provider data" not in result.content[0].text


def test_error_shape_is_stable() -> None:
    result = error_result(
        ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "bad input",
            schema_uri="dlsite://schema/dlsite_search",
        )
    )
    assert result.is_error is True
    assert result.structured_content == {
        "code": "INVALID_ARGUMENT",
        "message": "bad input",
        "retryable": False,
        "schema_uri": "dlsite://schema/dlsite_search",
        "details": {},
    }


def test_result_budget_is_enforced() -> None:
    envelope = collection_envelope(
        [{"text": "x" * 3000} for _ in range(20)],
        untrusted_fields=["items[].text"],
    )
    result = success_result(envelope, "Results returned.")
    assert compact_size(result.structured_content) <= 12 * 1024
