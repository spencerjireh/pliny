from dataclasses import dataclass
from typing import Literal

import httpx

Bucket = Literal["html", "pdf", "audio", "video"]

_USER_AGENT = "pliny/0.1 (+https://example.invalid/pliny)"


@dataclass(frozen=True)
class Classification:
    bucket: Bucket
    final_url: str
    content_type: str


def _bucket_for(content_type: str) -> Bucket:
    head = content_type.split(";", 1)[0].strip().lower()
    if head == "application/pdf":
        return "pdf"
    if head.startswith("audio/"):
        return "audio"
    if head.startswith("video/"):
        return "video"
    return "html"


async def classify_url(url: str, *, client: httpx.AsyncClient) -> Classification:
    """Resolve redirects and bucket by Content-Type.

    Tries HEAD first; falls back to a streaming GET when the origin blocks HEAD
    (some news sites return 405) or returns no Content-Type. Both paths follow
    redirects and surface the final URL and Content-Type.
    """
    headers = {"User-Agent": _USER_AGENT}
    final_url: str | None = None
    content_type: str | None = None
    try:
        resp = await client.head(url, headers=headers, follow_redirects=True)
        if resp.status_code < 400 and resp.headers.get("content-type"):
            final_url = str(resp.url)
            content_type = resp.headers["content-type"]
    except httpx.RequestError:
        pass

    if final_url is None or content_type is None:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as stream_resp:
            final_url = str(stream_resp.url)
            content_type = stream_resp.headers.get("content-type", "")

    assert final_url is not None
    assert content_type is not None
    return Classification(
        bucket=_bucket_for(content_type),
        final_url=final_url,
        content_type=content_type,
    )
