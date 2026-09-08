"""Validated schemas for the existing structuredContent wire format."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _compact_schema(schema: dict[str, Any]) -> None:
    # Generated labels/defaults add idle context without describing the wire data.
    schema.pop("title", None)
    for field in schema.get("properties", {}).values():
        field.pop("title", None)
        field.pop("default", None)


class OutputModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", json_schema_extra=_compact_schema)


class Page(OutputModel):
    returned: int = Field(ge=0, le=100)
    has_more: bool
    next_cursor: str | None


class SourceMeta(OutputModel):
    canonical_uri: str | None
    source: Literal["dlsite"]
    provider: str
    retrieved_at: str
    fresh_until: str | None
    quota_cost: None
    warnings: list[str]
    untrusted_fields: list[str]


class PartialData(OutputModel):
    # The byte-budget fallback may keep only a subset of provider fields, or
    # replace data with {"truncated": true}. Validation never fills those fields
    # into the response: Annotated[CallToolResult, Model] validates in place.
    truncated: bool | None = None


class WorkRecord(PartialData):
    product_id: str | None = None
    work_name: str | None = None
    description: str | None = None
    url: str | None = None
    review_id: str | None = None
    title: str | None = None
    review: str | None = None
    review_truncated: bool | None = None


class WorkData(WorkRecord):
    view: Literal["summary", "details"] | None = None
    product_name: str | None = None
    total_reviews_snapshot: int | None = Field(default=None, ge=0)
    position_start: int | None = Field(default=None, ge=0)
    position_end: int | None = Field(default=None, ge=0)
    complete: bool | None = None
    order: Literal["newest"] | None = None
    max_chars_per_review: int | None = Field(default=None, ge=100, le=4000)


class SearchData(PartialData):
    query: str | None = None
    site: str | None = None
    applied_filters: dict[str, str] | None = None
    result_sites: list[str] | None = None


class Price(OutputModel):
    # Compaction can remove a trailing field inside a nested object as well.
    amount: float | None = None
    currency: str | None = None


class SearchRecord(PartialData):
    product_id: str | None = None
    title: str | None = None
    maker_id: str | None = None
    maker_name: str | None = None
    url: str | None = None
    price: Price | None = None
    review_count: int | None = None


class MakerData(PartialData):
    maker_id: str | None = None
    maker_name: str | None = None
    maker_type: str | None = None
    url: str | None = None


class Envelope(OutputModel):
    schema_version: Literal["1"]
    kind: Literal["entity", "collection"]
    job: dict[str, Any] = Field(max_length=0)
    page: Page
    meta: SourceMeta


class WorkOutput(Envelope):
    data: WorkData
    items: list[WorkRecord] = Field(max_length=100)


class SearchOutput(Envelope):
    data: SearchData
    items: list[SearchRecord] = Field(max_length=100)


class MakerOutput(Envelope):
    kind: Literal["entity"]
    data: MakerData
    items: list[MakerData] = Field(max_length=0)
