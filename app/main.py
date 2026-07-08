"""FastAPI application factory and ASGI entry point.

Run locally with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.errors import register_error_handlers
from app.api.routes import admin, components, invoices, locations, stock, types
from app.db import init_db
from app.web import routes as web_routes

_STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(*, create_tables: bool = True) -> FastAPI:
    """Build and configure the ShelfOS FastAPI application.

    When ``create_tables`` is true, the schema is created on application
    startup (via the lifespan handler), not at import time — so importing this
    module in tests never touches a real database. Tests pass
    ``create_tables=False`` and bind their own in-memory engine.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if create_tables:
            init_db()
        yield

    app = FastAPI(title="ShelfOS", version="1.0.0", lifespan=lifespan)

    register_error_handlers(app)

    for module in (types, components, locations, stock, invoices, admin):
        app.include_router(module.router)

    app.include_router(web_routes.router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
