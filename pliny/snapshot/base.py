from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class SnapshotResult:
    """A single HTML page snapshot. The bytes returned in `rendered_html`
    are what get persisted as the canonical raw bytes for the item.
    """

    rendered_html: bytes
    screenshot_png: bytes
    final_url: str
    page_title: str | None
    fetched_at: datetime


class Snapshotter(Protocol):
    async def capture_html(self, url: str, *, timeout_s: float = 30.0) -> SnapshotResult: ...
