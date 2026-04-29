import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

RRF_K = 60


class MatchingChunk(BaseModel):
    chunk_id: UUID
    text: str
    score: float
    highlights: str


class SearchResultItem(BaseModel):
    id: UUID
    title: str | None
    summary: str | None
    type: str
    captured_at: datetime
    score: float | None = None
    matching_chunks: list[MatchingChunk] = []
    possible_duplicate_of: UUID | None = None


class SearchResponse(BaseModel):
    items: list[SearchResultItem]
    next_cursor: str | None = None


class QueryCursor(BaseModel):
    mode: Literal["q", "b"]
    score: float | None = None
    captured_at: datetime | None = None
    id: UUID


def encode_cursor(c: QueryCursor) -> str:
    return base64.urlsafe_b64encode(c.model_dump_json().encode()).decode()


def decode_cursor(s: str) -> QueryCursor:
    raw = base64.urlsafe_b64decode(s.encode()).decode()
    return QueryCursor.model_validate_json(raw)


@dataclass(frozen=True)
class RankedHit:
    item_id: UUID
    rank: int
    score: float
    chunk_id: UUID | None = None


def rrf_fuse(arms: list[list[RankedHit]]) -> dict[UUID, float]:
    """Reciprocal-rank fusion. Returns {item_id: rrf_score}.

    Chunk-arm hits aggregate up to their item by summing contributions.
    """
    out: dict[UUID, float] = {}
    for arm in arms:
        for hit in arm:
            out[hit.item_id] = out.get(hit.item_id, 0.0) + 1.0 / (RRF_K + hit.rank)
    return out
