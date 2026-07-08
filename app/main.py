"""FastAPI application factory and ASGI entry point.

Run locally with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import admin, components, invoices, locations, stock, types
from app.db import init_db


def create_app(*, create_tables: bool = True) -> FastAPI:
    """Build and configure the ShelfOS FastAPI application."""
    app = FastAPI(title="ShelfOS", version="1.0.0")

    if create_tables:
        init_db()

    register_error_handlers(app)

    for module in (types, components, locations, stock, invoices, admin):
        app.include_router(module.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app(create_tables=False)
