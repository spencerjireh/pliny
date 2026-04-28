import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from pliny.api import deps
from pliny.config import get_settings

NEO4J_TEST_PASSWORD = "testpass"


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


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    container = Neo4jContainer("neo4j:5", password=NEO4J_TEST_PASSWORD)
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def test_neo4j_uri(neo4j_container: Neo4jContainer) -> str:
    return neo4j_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _env_override(
    test_database_url: str,
    test_neo4j_uri: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    blob_root = tmp_path_factory.mktemp("blob")
    prev = {
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "API_KEY": os.environ.get("API_KEY"),
        "BLOB_ROOT": os.environ.get("BLOB_ROOT"),
        "OPENAI_DAILY_USD_CAP": os.environ.get("OPENAI_DAILY_USD_CAP"),
        "NEO4J_URI": os.environ.get("NEO4J_URI"),
        "NEO4J_USER": os.environ.get("NEO4J_USER"),
        "NEO4J_PASSWORD": os.environ.get("NEO4J_PASSWORD"),
    }
    os.environ["DATABASE_URL"] = test_database_url
    os.environ["API_KEY"] = "test-key"
    os.environ["BLOB_ROOT"] = str(blob_root)
    os.environ["OPENAI_DAILY_USD_CAP"] = "100"
    os.environ["NEO4J_URI"] = test_neo4j_uri
    os.environ["NEO4J_USER"] = "neo4j"
    os.environ["NEO4J_PASSWORD"] = NEO4J_TEST_PASSWORD
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


@pytest.fixture
async def neo4j_driver() -> AsyncIterator[Any]:
    from pliny.db import neo4j as neo4j_mod
    from pliny.graph import schema as graph_schema

    neo4j_mod.force_reset_driver_for_tests()
    graph_schema.reset_ensured_for_tests()
    drv = neo4j_mod.get_driver()
    yield drv


@pytest.fixture(autouse=True)
async def _reset_neo4j(neo4j_driver: Any) -> None:
    async with neo4j_driver.session() as s:
        await s.run("MATCH (n) DETACH DELETE n")


ChatResponseProvider = Any  # callable[[dict], str] | None


class FakeLLM:
    """Test double for the LLM Protocol. Records calls and returns canned responses."""

    def __init__(self) -> None:
        self.vision_calls: list[dict[str, object]] = []
        self.vision_response_text: str = "OCR:\n(none)\n\nCAPTION:\nA test image."
        self.chat_calls: list[dict[str, object]] = []
        self.chat_response_text: str = (
            '{"title":"Test Item","summary":"Test summary.","tags":["test","example"]}'
        )
        self.entities_response_text: str = (
            '{"entities":[{"name":"Test Entity","type":"concept",'
            '"mention_text":"test","confidence":0.9}]}'
        )
        self.chat_response_provider: ChatResponseProvider = None
        self.embed_calls: list[dict[str, object]] = []
        self.embed_response_vectors: list[list[float]] | None = None

    async def chat(self, **kwargs: object) -> object:
        from pliny.llm.base import ChatResponse

        self.chat_calls.append(kwargs)
        if self.chat_response_provider is not None:
            text = self.chat_response_provider(kwargs)
        else:
            text = self.chat_response_text
        return ChatResponse(
            text=text,
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
