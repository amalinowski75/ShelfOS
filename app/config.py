"""Runtime configuration read from environment variables (decision D11).

Sensible insecure defaults are provided for local development; production
deployments should override the secret and admin password via the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

# Deployment environment. Anything other than "production" is treated as a
# development/test context where the insecure defaults below are tolerated.
ENV = os.environ.get("SHELFOS_ENV", "development").strip().lower()

# Signs both session cookies and JWT API tokens. At least 32 bytes so HS256 is
# happy; still insecure and must be overridden in production.
_DEFAULT_SECRET = "shelfos-dev-insecure-secret-change-me-in-production"
SECRET_KEY = os.environ.get("SHELFOS_SECRET_KEY", _DEFAULT_SECRET)

# Bootstrap admin seeded on first startup if no admin exists.
ADMIN_USERNAME = os.environ.get("SHELFOS_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SHELFOS_ADMIN_PASSWORD", "admin")

# JWT access-token lifetime, in hours.
TOKEN_EXPIRE_HOURS = int(os.environ.get("SHELFOS_TOKEN_EXPIRE_HOURS", "24"))

# On-disk store for uploaded attachments (spec §10); the DB keeps only metadata
# and the stored path. Relative to the CWD by default (git-ignored), created
# lazily on first write. Referenced as ``config.ATTACHMENTS_DIR`` at call time so
# tests can point it at a tmp dir.
ATTACHMENTS_DIR = Path(os.environ.get("SHELFOS_ATTACHMENTS_DIR", "attachments"))

# Reject uploads larger than this (the whole file is buffered in memory).
MAX_ATTACHMENT_MB = int(os.environ.get("SHELFOS_MAX_ATTACHMENT_MB", "25"))
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

# Longest edge (px) of generated image thumbnails; cached on disk next to the
# originals under ATTACHMENTS_DIR/.thumbs.
THUMBNAIL_PX = int(os.environ.get("SHELFOS_THUMBNAIL_PX", "240"))

# BOM import: a stock part is offered as a "close" substitute for a passive
# (R/C/L) when its value is within this percent of the BOM line's value (§21).
SUBSTITUTE_TOLERANCE_PCT = float(
    os.environ.get("SHELFOS_SUBSTITUTE_TOLERANCE_PCT", "10")
)


def is_production() -> bool:
    """True when running in a production deployment (D11)."""
    return ENV == "production"


def is_using_default_secret() -> bool:
    """True when the (insecure) default secret key is in effect."""
    return SECRET_KEY == _DEFAULT_SECRET


def is_using_default_admin_password() -> bool:
    """True when the default admin password is in effect."""
    return ADMIN_PASSWORD == "admin"

# Server-side fetch of an attachment from a URL (spec §10): connect+read timeout
# (seconds) and the maximum number of redirects followed (each re-validated).
ATTACHMENT_URL_TIMEOUT = float(os.environ.get("SHELFOS_ATTACHMENT_URL_TIMEOUT", "10"))
# Hard wall-clock ceiling for the whole fetch (all hops), independent of the
# per-read timeout above, so a slow-trickle server can't hold a worker thread.
ATTACHMENT_URL_TOTAL_TIMEOUT = float(
    os.environ.get("SHELFOS_ATTACHMENT_URL_TOTAL_TIMEOUT", "30")
)
ATTACHMENT_URL_MAX_REDIRECTS = int(
    os.environ.get("SHELFOS_ATTACHMENT_URL_MAX_REDIRECTS", "5")
)
# Cap concurrent URL fetches so a burst of slow downloads can't exhaust the sync
# worker-thread pool and stall unrelated endpoints.
ATTACHMENT_URL_MAX_CONCURRENCY = int(
    os.environ.get("SHELFOS_ATTACHMENT_URL_MAX_CONCURRENCY", "4")
)
# Some CDNs/WAFs (e.g. Akamai in front of st.com) tarpit non-browser clients, so
# the default python-httpx UA hangs instead of downloading. Present as a browser.
ATTACHMENT_URL_USER_AGENT = os.environ.get(
    "SHELFOS_ATTACHMENT_URL_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

# Shop-integration API keys (spec: create component from a shop URL). Keys live in
# the environment, never in the DB. The Mouser Search API key is optional; the
# feature is disabled until it's set.
# .strip() so a stray newline/space from `export` doesn't silently invalidate it
# (Mouser answers "Invalid unique identifier." for any malformed key).
MOUSER_API_KEY = os.environ.get("SHELFOS_MOUSER_API_KEY", "").strip()
SHOP_API_TIMEOUT = float(os.environ.get("SHELFOS_SHOP_API_TIMEOUT", "10"))

# Digi-Key uses OAuth2 client-credentials: an ID/secret pair buys a short-lived
# token. API_BASE is overridable to point at their sandbox; the locale headers are
# required by the product endpoint and only affect pricing/description language.
DIGIKEY_CLIENT_ID = os.environ.get("SHELFOS_DIGIKEY_CLIENT_ID", "").strip()
DIGIKEY_CLIENT_SECRET = os.environ.get("SHELFOS_DIGIKEY_CLIENT_SECRET", "").strip()
DIGIKEY_API_BASE = os.environ.get(
    "SHELFOS_DIGIKEY_API_BASE", "https://api.digikey.com"
).rstrip("/")
DIGIKEY_LOCALE_SITE = os.environ.get("SHELFOS_DIGIKEY_LOCALE_SITE", "US")
DIGIKEY_LOCALE_LANGUAGE = os.environ.get("SHELFOS_DIGIKEY_LOCALE_LANGUAGE", "en")
DIGIKEY_LOCALE_CURRENCY = os.environ.get("SHELFOS_DIGIKEY_LOCALE_CURRENCY", "USD")

# TME API v2: OAuth2 client-credentials, but the pair goes out as HTTP Basic (the
# 50-character token is the username, the 20-character application secret the
# password). Both are generated at developers.tme.eu.
TME_TOKEN = os.environ.get("SHELFOS_TME_TOKEN", "").strip()
TME_SECRET = os.environ.get("SHELFOS_TME_SECRET", "").strip()
TME_API_BASE = os.environ.get("SHELFOS_TME_API_BASE", "https://api.tme.eu").rstrip("/")
TME_COUNTRY = os.environ.get("SHELFOS_TME_COUNTRY", "PL")
# Sent as Accept-Language. Keep "en": a translated locale also translates parameter
# *names*, which then stop matching ShelfOS's English parameter labels and get
# dropped (the same trap Digi-Key's locale language has).
TME_LANGUAGE = os.environ.get("SHELFOS_TME_LANGUAGE", "en")

# A visible field separator some barcode scanners emit in place of the ISO 15434
# group separator (GS, 0x1D). The scan parser always accepts GS/RS; set this if your
# scanner is configured to send a printable one (e.g. "|") instead.
SCAN_SEPARATOR = os.environ.get("SHELFOS_SCAN_SEPARATOR", "").strip()
