from fastapi import APIRouter, FastAPI

from pliny.api.routes import health, items
from pliny.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Pliny", version="0.1.0")

    app.include_router(health.router, tags=["health"])

    v1 = APIRouter(prefix="/v1")
    v1.include_router(items.router, prefix="/items", tags=["items"])
    app.include_router(v1)

    return app
