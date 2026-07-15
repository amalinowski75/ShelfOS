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


def is_production() -> bool:
    """True when running in a production deployment (D11)."""
    return ENV == "production"


def is_using_default_secret() -> bool:
    """True when the (insecure) default secret key is in effect."""
    return SECRET_KEY == _DEFAULT_SECRET


def is_using_default_admin_password() -> bool:
    """True when the default admin password is in effect."""
    return ADMIN_PASSWORD == "admin"
