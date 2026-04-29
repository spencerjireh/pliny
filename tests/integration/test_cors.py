import os
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pliny.api import deps
from pliny.config import get_settings


@pytest.fixture
async def cors_app() -> AsyncIterator[FastAPI]:
    """Build a fresh app with CORS configured, then restore env."""
    prev = os.environ.get("CORS_ALLOWED_ORIGINS")
    os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost:5173"
    get_settings.cache_clear()
    deps.reset_state()
    try:
        from pliny.api.app import create_app

        yield create_app()
    finally:
        if prev is None:
            os.environ.pop("CORS_ALLOWED_ORIGINS", None)
        else:
            os.environ["CORS_ALLOWED_ORIGINS"] = prev
        get_settings.cache_clear()
        deps.reset_state()


async def test_cors_preflight_allowed(cors_app: FastAPI) -> None:
    transport = ASGITransport(app=cors_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.options(
            "/v1/items",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "GET" in r.headers["access-control-allow-methods"]


async def test_cors_disallowed_origin_has_no_acao(cors_app: FastAPI) -> None:
    transport = ASGITransport(app=cors_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.options(
            "/v1/items",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert r.headers.get("access-control-allow-origin") != "http://evil.example.com"


async def test_cors_disabled_when_env_empty(client: AsyncClient) -> None:
    # The shared `client` fixture is built without CORS configured (default empty).
    r = await client.options(
        "/v1/items",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Without the middleware, OPTIONS is unhandled (FastAPI returns 405) and no CORS
    # headers are emitted.
    assert "access-control-allow-origin" not in r.headers
