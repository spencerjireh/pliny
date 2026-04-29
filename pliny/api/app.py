from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pliny.api.routes import admin, health, items, metrics, search
from pliny.config import get_settings
from pliny.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Pliny", version="0.1.0")

    settings = get_settings()
    if settings.cors_allowed_origins:
        origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=False,
        )

    app.include_router(health.router, tags=["health"])
    app.include_router(metrics.router, tags=["metrics"])

    v1 = APIRouter(prefix="/v1")
    v1.include_router(items.router, prefix="/items", tags=["items"])
    v1.include_router(search.router, prefix="/search", tags=["search"])
    v1.include_router(admin.router, prefix="/admin", tags=["admin"])
    app.include_router(v1)

    return app
