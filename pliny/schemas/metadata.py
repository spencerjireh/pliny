from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ItemMetadataBase(BaseModel):
    """Common metadata fields. Per-type subclasses extend this.

    See spec.md "items.metadata Discipline" — readers tolerate missing keys; writers
    must go through these models.
    """

    model_config = ConfigDict(extra="allow")

    forwarded_from: dict[str, Any] | None = None
    source_url: str | None = None
    mime: str | None = None
    possible_duplicate_of: str | None = None


class TextMeta(ItemMetadataBase):
    type: Literal["text"] = "text"


class UrlMeta(ItemMetadataBase):
    type: Literal["url"] = "url"
    og: dict[str, Any] | None = None
    final_url: str | None = None
    paywalled: bool | None = None
    bot_challenge: bool | None = None
    archive_source: str | None = None
    archive_timestamp: str | None = None
    redirect_resolved_to: str | None = None


class ImageMeta(ItemMetadataBase):
    type: Literal["image"] = "image"
    exif: dict[str, Any] | None = None


class PdfMeta(ItemMetadataBase):
    type: Literal["pdf"] = "pdf"
    chunk_overflow: bool | None = None
    original_chunk_count: int | None = None


class AudioMeta(ItemMetadataBase):
    type: Literal["audio"] = "audio"
    media_host: str | None = None
    duration_seconds: float | None = None
    channel: str | None = None
    show: str | None = None
    published_at: str | None = None
    thumbnail_url: str | None = None
    captions_available: bool | None = None


class VideoMeta(ItemMetadataBase):
    type: Literal["video"] = "video"
    media_host: str | None = None
    duration_seconds: float | None = None
    channel: str | None = None
    show: str | None = None
    published_at: str | None = None
    thumbnail_url: str | None = None
    captions_available: bool | None = None


class FileMeta(ItemMetadataBase):
    type: Literal["file"] = "file"
