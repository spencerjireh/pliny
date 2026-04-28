from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class IngestJSON(BaseModel):
    text: str | None = None
    url: str | None = None
    source: str = Field(min_length=1)
    source_ref: str | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def at_least_one_payload(self) -> "IngestJSON":
        if not self.text and not self.url:
            raise ValueError("at least one of 'text' or 'url' is required")
        return self


class IngestItemResult(BaseModel):
    item_id: UUID
    type: str
    deduplicated: bool


class IngestResponse(BaseModel):
    items: list[IngestItemResult]
