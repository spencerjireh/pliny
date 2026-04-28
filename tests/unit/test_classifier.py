import httpx
import pytest
import respx

from pliny.snapshot.classifier import _bucket_for, classify_url


def test_bucket_for_pdf() -> None:
    assert _bucket_for("application/pdf") == "pdf"
    assert _bucket_for("Application/PDF; charset=utf-8") == "pdf"


def test_bucket_for_audio_video() -> None:
    assert _bucket_for("audio/mpeg") == "audio"
    assert _bucket_for("video/mp4") == "video"


def test_bucket_for_html_default() -> None:
    assert _bucket_for("text/html") == "html"
    assert _bucket_for("application/xhtml+xml") == "html"
    assert _bucket_for("") == "html"
    assert _bucket_for("application/json") == "html"


@respx.mock
@pytest.mark.asyncio
async def test_classify_url_html_via_head() -> None:
    respx.head("https://example.com/article").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
    )
    async with httpx.AsyncClient() as client:
        result = await classify_url("https://example.com/article", client=client)
    assert result.bucket == "html"
    assert result.final_url == "https://example.com/article"
    assert result.content_type == "text/html; charset=utf-8"


@respx.mock
@pytest.mark.asyncio
async def test_classify_url_pdf_via_head() -> None:
    respx.head("https://example.com/file.pdf").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/pdf"})
    )
    async with httpx.AsyncClient() as client:
        result = await classify_url("https://example.com/file.pdf", client=client)
    assert result.bucket == "pdf"
    assert result.content_type == "application/pdf"


@respx.mock
@pytest.mark.asyncio
async def test_classify_url_follows_redirects() -> None:
    respx.head("https://example.com/short").mock(
        return_value=httpx.Response(301, headers={"location": "https://example.com/long"})
    )
    respx.head("https://example.com/long").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    async with httpx.AsyncClient() as client:
        result = await classify_url("https://example.com/short", client=client)
    assert result.final_url == "https://example.com/long"
    assert result.bucket == "html"


@respx.mock
@pytest.mark.asyncio
async def test_classify_url_falls_back_to_get_when_head_blocked() -> None:
    respx.head("https://example.com/strict").mock(return_value=httpx.Response(405))
    respx.get("https://example.com/strict").mock(
        return_value=httpx.Response(200, headers={"content-type": "audio/mpeg"})
    )
    async with httpx.AsyncClient() as client:
        result = await classify_url("https://example.com/strict", client=client)
    assert result.bucket == "audio"
    assert result.final_url == "https://example.com/strict"


@respx.mock
@pytest.mark.asyncio
async def test_classify_url_falls_back_when_head_lacks_content_type() -> None:
    respx.head("https://example.com/x").mock(return_value=httpx.Response(200))
    respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, headers={"content-type": "video/mp4"})
    )
    async with httpx.AsyncClient() as client:
        result = await classify_url("https://example.com/x", client=client)
    assert result.bucket == "video"
