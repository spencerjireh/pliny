import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.postgres import PostgresContainer

from pliny.api import deps
from pliny.config import get_settings


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("pgvector/pgvector:pg15", driver="psycopg")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def test_database_url(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _env_override(
    test_database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    blob_root = tmp_path_factory.mktemp("blob")
    prev = {
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "API_KEY": os.environ.get("API_KEY"),
        "BLOB_ROOT": os.environ.get("BLOB_ROOT"),
        "OPENAI_DAILY_USD_CAP": os.environ.get("OPENAI_DAILY_USD_CAP"),
    }
    os.environ["DATABASE_URL"] = test_database_url
    os.environ["API_KEY"] = "test-key"
    os.environ["BLOB_ROOT"] = str(blob_root)
    os.environ["OPENAI_DAILY_USD_CAP"] = "100"
    get_settings.cache_clear()
    deps.reset_state()
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    deps.reset_state()


@pytest.fixture(scope="session", autouse=True)
def _run_migrations(test_database_url: str, _env_override: None) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", test_database_url)
    command.upgrade(cfg, "head")


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    from pliny.api.app import create_app

    application = create_app()
    yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key"}


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    sm = deps.get_session_maker()
    async with sm() as session:
        yield session


class FakeLLM:
    """Test double for the LLM Protocol. Records calls and returns canned responses."""

    def __init__(self) -> None:
        self.vision_calls: list[dict[str, object]] = []
        self.vision_response_text: str = "OCR:\n(none)\n\nCAPTION:\nA test image."
        self.chat_calls: list[dict[str, object]] = []
        self.chat_response_text: str = (
            '{"title":"Test Item","summary":"Test summary.","tags":["test","example"]}'
        )
        self.embed_calls: list[dict[str, object]] = []
        self.embed_response_vectors: list[list[float]] | None = None

    async def chat(self, **kwargs: object) -> object:
        from pliny.llm.base import ChatResponse

        self.chat_calls.append(kwargs)
        return ChatResponse(
            text=self.chat_response_text,
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        self.embed_calls.append({"texts": list(texts), "model": model})
        if self.embed_response_vectors is not None:
            return [list(v) for v in self.embed_response_vectors[: len(texts)]]
        return [[0.0] * 1536 for _ in texts]

    async def vision(self, **kwargs: object) -> object:
        from pliny.llm.base import ChatResponse

        self.vision_calls.append(kwargs)
        return ChatResponse(
            text=self.vision_response_text,
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
