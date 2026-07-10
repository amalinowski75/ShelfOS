"""Runtime configuration read from environment variables (decision D11).

Sensible insecure defaults are provided for local development; production
deployments should override the secret and admin password via the environment.
"""

from __future__ import annotations

import os

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


def is_production() -> bool:
    """True when running in a production deployment (D11)."""
    return ENV == "production"


def is_using_default_secret() -> bool:
    """True when the (insecure) default secret key is in effect."""
    return SECRET_KEY == _DEFAULT_SECRET


def is_using_default_admin_password() -> bool:
    """True when the default admin password is in effect."""
    return ADMIN_PASSWORD == "admin"
