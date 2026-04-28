from fastapi import FastAPI

from pliny.api.routes import health
from pliny.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Pliny", version="0.1.0")

    app.include_router(health.router, tags=["health"])

    v1 = FastAPI(title="Pliny v1", version="0.1.0")
    app.mount("/v1", v1)
    app.state.v1 = v1

    return app
