"""FastAPI application factory and ASGI entry point.

Run locally with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.api.errors import register_error_handlers
from app.api.routes import (
    admin,
    auth,
    components,
    invoices,
    locations,
    stock,
    types,
)
from app.auth.deps import require_access, require_admin
from app.db import engine, init_db
from app.seed import ensure_system_user
from app.services import user_service as us
from app.web import routes as web_routes

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_logger = logging.getLogger("shelfos")

# Business routers require authentication and enforce read-only write blocking.
_PROTECTED_ROUTERS = (types, components, locations, stock, invoices)


def _bootstrap() -> None:
    """Create the schema and seed the system user and bootstrap admin (D11)."""
    _check_insecure_defaults()
    init_db()
    with Session(engine) as session:
        ensure_system_user(session)
        us.ensure_admin(
            session,
            username=config.ADMIN_USERNAME,
            password=config.ADMIN_PASSWORD,
        )


def _check_insecure_defaults() -> None:
    """Refuse to start with insecure defaults in production; warn otherwise (D11).

    The default secret signs both JWT API tokens and session cookies, so leaving
    it in place lets anyone who knows the (public) default forge an admin token.
    In production this is fatal; in development it is only a warning.
    """
    if config.is_using_default_secret():
        if config.is_production():
            raise RuntimeError(
                "Refusing to start: SHELFOS_SECRET_KEY must be set when "
                "SHELFOS_ENV=production (the default secret is public and lets "
                "anyone forge admin tokens)."
            )
        _logger.warning(
            "Using the default SECRET_KEY; set SHELFOS_SECRET_KEY in production."
        )
    if config.is_using_default_admin_password():
        if config.is_production():
            raise RuntimeError(
                "Refusing to start: SHELFOS_ADMIN_PASSWORD must be set when "
                "SHELFOS_ENV=production (the default admin password is public)."
            )
        _logger.warning(
            "Bootstrap admin uses the default password; "
            "set SHELFOS_ADMIN_PASSWORD and change it."
        )


def create_app(*, create_tables: bool = True) -> FastAPI:
    """Build and configure the ShelfOS FastAPI application.

    When ``create_tables`` is true, the schema is created and seeded on startup
    (via the lifespan handler), not at import time — so importing this module in
    tests never touches a real database. Tests pass ``create_tables=False`` and
    bind their own in-memory engine.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if create_tables:
            _bootstrap()
        yield

    app = FastAPI(title="ShelfOS", version="1.0.0", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

    register_error_handlers(app)

    # Public: authentication endpoints.
    app.include_router(auth.router)

    # Authenticated business endpoints (read-only accounts blocked on writes).
    for module in _PROTECTED_ROUTERS:
        app.include_router(module.router, dependencies=[Depends(require_access)])

    # Admin-only endpoints.
    app.include_router(admin.router, dependencies=[Depends(require_admin)])

    app.include_router(web_routes.router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
