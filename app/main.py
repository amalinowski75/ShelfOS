"""FastAPI application factory and ASGI entry point.

Run locally with::

    uvicorn app.main:app --reload --port 9000
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.api.errors import register_error_handlers
from app.api.routes import (
    admin,
    attachments,
    auth,
    bom_takes,
    boms,
    components,
    invoices,
    labels,
    links,
    locations,
    manufacturers,
    matching,
    shops,
    stock,
    types,
)
from app.auth.deps import require_access, require_admin, require_csrf
from app.auth.throttle import LoginThrottle
from app.db import engine, init_db
from app.services import label_printer, match_rule_service
from app.services import user_service as us
from app.services.errors import ValidationError
from app.services.shops import scan
from app.web import routes as web_routes

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_logger = logging.getLogger("shelfos")

# Business routers require authentication and enforce read-only write blocking.
_PROTECTED_ROUTERS = (
    types,
    components,
    locations,
    stock,
    invoices,
    attachments,
    boms,
    bom_takes,
    shops,
    matching,
    manufacturers,
    links,
    labels,
)


def _bootstrap() -> None:
    """Create the schema and seed the bootstrap admin (D11).

    Not the account demo data is attributed to: that belongs to the demo data
    and is created with it (see :mod:`app.seed`).
    """
    _check_insecure_defaults()
    _check_scan_separator()
    _check_label_settings()
    init_db()
    with Session(engine) as session:
        _check_admin_password_source(session)
        us.ensure_admin(
            session,
            username=config.ADMIN_USERNAME,
            password=config.ADMIN_PASSWORD,
        )
        _check_seeded_admin_password(session)
        match_rule_service.seed_default_rules(session)


def _check_admin_password_source(session: Session) -> None:
    """Judge ``SHELFOS_ADMIN_PASSWORD``, but only where it is about to be used.

    It is the password of an admin this startup is about to create, and nothing
    else: ``ensure_admin`` seeds only when no login-capable admin exists, so on
    every later start the variable is inert. Refusing production over an inert
    setting would mean an operator who fixed the real account and then dropped
    the pointless variable — which is what the README now tells them it is —
    could not boot until they set a decoy value no account uses. Where it will
    not be read, it is not worth an opinion; the account is judged either way
    by :func:`_check_seeded_admin_password`.
    """
    if us.has_login_capable_admin(session):
        return
    if config.is_using_default_admin_password():
        if config.is_production():
            raise RuntimeError(
                "Refusing to start: SHELFOS_ADMIN_PASSWORD must be set when "
                "SHELFOS_ENV=production, because this database has no admin yet "
                "and the default password is public."
            )
        _logger.warning(
            "Bootstrap admin will use the default password; "
            "set SHELFOS_ADMIN_PASSWORD and change it."
        )
    elif len(config.ADMIN_PASSWORD) < us.MIN_PASSWORD_LENGTH:
        # The bootstrap admin is seeded past the password policy (it has to be,
        # for the development default above), so the policy is applied to its
        # source here instead: the one account an attacker knows exists should
        # not be the one with the weakest password.
        if config.is_production():
            raise RuntimeError(
                "Refusing to start: SHELFOS_ADMIN_PASSWORD must be at least "
                f"{us.MIN_PASSWORD_LENGTH} characters when SHELFOS_ENV=production."
            )
        _logger.warning(
            "SHELFOS_ADMIN_PASSWORD is shorter than %d characters; "
            "set a longer one before exposing this instance.",
            us.MIN_PASSWORD_LENGTH,
        )


def _check_seeded_admin_password(session: Session) -> None:
    """Refuse to start in production while an admin still has the default password.

    ``_check_insecure_defaults`` asks whether ``SHELFOS_ADMIN_PASSWORD`` is set,
    which turns out to answer a different question. ``ensure_admin`` only seeds
    when there is no login-capable admin yet, so on a database that already has
    one — every install past its first run — setting the variable changes
    nothing about the account. An instance could therefore be configured
    correctly, pass every check, and still be open to ``admin``/``admin``, with
    the startup log saying nothing at all. So ask the accounts instead.
    """
    exposed = us.admins_with_password(session, config.DEFAULT_ADMIN_PASSWORD)
    if not exposed:
        return
    names = ", ".join(sorted(user.name for user in exposed))
    if config.is_production():
        # The script first, and only the script: this branch stops the app from
        # starting, so there is no instance to sign in to and no Change password
        # button to reach. Naming that remedy first would send the reader to a
        # port with nothing listening on it.
        raise RuntimeError(
            f"Refusing to start: admin account(s) {names} still have the default "
            "password, which is public. With the app stopped, run "
            "`sudo ./shelfos.sh password <username>` — or, running the script "
            "directly, `python scripts/set_password.py <username>` with "
            "DATABASE_URL set to this instance's database, which is what the "
            "wrapper does for you. Setting SHELFOS_ADMIN_PASSWORD does not "
            "change an account that already exists."
        )
    # Here the app does start, so the ordinary way round is the ordinary advice.
    _logger.warning(
        "Admin account(s) %s still have the default password. Sign in and use "
        "Change password, or stop the app and run "
        "`./shelfos.sh password <username>`.",
        names,
    )


def _check_scan_separator() -> None:
    """Say when a configured barcode separator is being ignored.

    The parser refuses a separator that could occur inside a field, because
    splitting on one would import confidently wrong data. Doing that silently is
    indistinguishable from the setting having no effect, so name it once at startup.
    """
    if config.SCAN_SEPARATOR and scan.configured_separator() is None:
        _logger.warning(
            "Ignoring SHELFOS_SCAN_SEPARATOR=%r: it must be a single character that "
            "cannot occur inside a barcode field (not a letter, digit, or -._/+). "
            "Scans will still be split on the standard GS/RS separators.",
            config.SCAN_SEPARATOR,
        )


def _check_label_settings() -> None:
    """Say when the label settings would fail the first time someone prints.

    Never fatal: labels are optional, and a bad setting costs a 422 on one
    request, not a broken deployment. But the failure would otherwise surface
    at the printer, in front of a user holding a roll of tape, so name the cause
    at startup instead — the same reasoning as the scan separator above.
    """
    try:
        geometry = label_printer.tape_geometry()
    except ValidationError as error:
        _logger.warning("SHELFOS_LABEL_TAPE/LENGTH is unusable: %s", error)
        return
    if not geometry.endless and "SHELFOS_LABEL_LENGTH_MM" in os.environ:
        _logger.warning(
            "Ignoring SHELFOS_LABEL_LENGTH_MM=%r: tape %r is die-cut, so its "
            "length is fixed by the die (%d dots).",
            config.LABEL_LENGTH_MM,
            geometry.tape,
            geometry.length_px,
        )
    try:
        label_printer.font_paths()
    except ValidationError as error:
        _logger.warning("Labels cannot be rendered: %s", error)
    if config.LABEL_STATUS_TIMEOUT <= 0:
        # A supported way to say "do not talk to the printer", so it is stated
        # rather than warned about — but stated, because it turns off the checks
        # that would otherwise catch a wrong tape before it is printed on.
        _logger.info(
            "SHELFOS_LABEL_STATUS_TIMEOUT=%r: not asking the printer anything, "
            "so jobs are sent unchecked and reported as sent, not printed.",
            config.LABEL_STATUS_TIMEOUT,
        )
    if config.LABEL_PRINT_TIMEOUT <= 0:
        # Not a mode: a negative wait for the lock is an endless one, and a
        # non-positive write budget fails every job before a byte leaves.
        _logger.warning(
            "SHELFOS_LABEL_PRINT_TIMEOUT=%r is not a positive number of "
            "seconds; printing will be refused until it is.",
            config.LABEL_PRINT_TIMEOUT,
        )
    if not config.label_printing_configured():
        return  # no printer is a normal setup, not a misconfiguration
    device = Path(config.LABEL_DEVICE)
    if not device.exists():
        _logger.warning(
            "SHELFOS_LABEL_DEVICE=%r does not exist; printing will fail until the "
            "printer is plugged in (a Brother QL on USB is usually /dev/usb/lp0).",
            config.LABEL_DEVICE,
        )
    elif not os.access(device, os.W_OK):
        _logger.warning(
            "SHELFOS_LABEL_DEVICE=%r is not writable; add the ShelfOS user to the "
            "'lp' group, or install a udev rule for the printer.",
            config.LABEL_DEVICE,
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

    # The interactive docs and the schema they read are served by
    # ``app.web.routes`` instead, behind the admin session — an inventory of
    # every endpoint and its shape is a map for someone probing the instance,
    # and it costs an admin nothing to sign in first. Disabled here rather than
    # guarded because FastAPI builds these routes at construction, with no
    # dependency of ours on them.
    app = FastAPI(
        title="ShelfOS",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Failed sign-in counters live on the app, not the module, so every app
    # instance (each test's included) starts with a clean slate.
    app.state.login_throttle = LoginThrottle(
        limit=config.LOGIN_MAX_FAILURES,
        window=config.LOGIN_FAILURE_WINDOW_SECONDS,
    )
    # The session cookie must never travel over plain HTTP in production; keep it
    # SameSite=Lax so cross-site POSTs don't carry it (defence alongside the CSRF
    # token enforced by require_csrf).
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.SECRET_KEY,
        https_only=config.is_production(),
        same_site="lax",
    )

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Set baseline security response headers on every response.

        ``setdefault`` so a route that sets its own (e.g. the attachment download
        already sends ``nosniff``) is not overridden.

        Known gap: a truly unhandled exception is turned into a 500 by Starlette's
        ServerErrorMiddleware, which wraps this middleware, so that response skips
        these headers. The body is a static ``debug=False`` message with no
        reflected content; a fronting reverse proxy should set these for full
        coverage in production.
        """
        response = await call_next(request)
        # Refuse framing (clickjacking): X-Frame-Options for old browsers,
        # CSP frame-ancestors for modern ones.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        # HSTS only in production — it is meaningful over HTTPS and would pin
        # localhost to HTTPS in dev otherwise.
        if config.is_production():
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response

    register_error_handlers(app)

    # Public: authentication endpoints.
    app.include_router(auth.router)

    # Authenticated business endpoints (read-only accounts blocked on writes,
    # cookie-authenticated writes require a CSRF token).
    for module in _PROTECTED_ROUTERS:
        app.include_router(
            module.router,
            dependencies=[Depends(require_access), Depends(require_csrf)],
        )

    # Admin-only endpoints.
    app.include_router(
        admin.router, dependencies=[Depends(require_admin), Depends(require_csrf)]
    )

    app.include_router(web_routes.router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
