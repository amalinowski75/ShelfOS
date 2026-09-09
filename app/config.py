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

# Bootstrap admin seeded on first startup if no admin exists. Both the seeding
# and the startup check that the seeded account is no longer on this password
# need the default by name, so it is one constant rather than two literals.
DEFAULT_ADMIN_PASSWORD = "admin"
ADMIN_USERNAME = os.environ.get("SHELFOS_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SHELFOS_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)

# JWT access-token lifetime, in hours.
TOKEN_EXPIRE_HOURS = int(os.environ.get("SHELFOS_TOKEN_EXPIRE_HOURS", "24"))

# Sign-in throttle (brute-force guard): after this many failed sign-ins from one
# client address within the window, that address is refused (429) until the
# oldest failure has aged out. Set the count to 0 to turn the throttle off.
LOGIN_MAX_FAILURES = int(os.environ.get("SHELFOS_LOGIN_MAX_FAILURES", "10"))
LOGIN_FAILURE_WINDOW_SECONDS = float(
    os.environ.get("SHELFOS_LOGIN_FAILURE_WINDOW_SECONDS", "900")
)

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


def _flag(name: str, default: bool) -> bool:
    """A yes/no setting, read the way people write them."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cookie_secure() -> bool:
    """Whether the session cookie is marked ``Secure``.

    Production implies yes, and that is right whenever there is TLS in front. It
    is not right when there is not: a Secure cookie is never sent back over plain
    HTTP, so the browser silently drops the session, the sign-in form's token has
    nothing to match, and the only symptom is "that sign-in form has expired" for
    ever — with nothing in the log, because nothing failed.

    A deployment served over plain HTTP on purpose (a test box, a private
    bridge) sets ``SHELFOS_COOKIE_SECURE=0`` and gets a working sign-in and the
    honest consequence: on plain HTTP the cookie travels in the clear, exactly
    like the password that established it.
    """
    return _flag("SHELFOS_COOKIE_SECURE", is_production())


def is_using_default_secret() -> bool:
    """True when the (insecure) default secret key is in effect."""
    return SECRET_KEY == _DEFAULT_SECRET


def is_using_default_admin_password() -> bool:
    """True when the default admin password is in effect."""
    return ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD


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

# element14 (Farnell / Newark / CPC): one API key as a query parameter, like Mouser.
# The store decides currency, stock — and the LANGUAGE OF THE ATTRIBUTE LABELS, which
# is why the default is a UK one rather than the nearest. A localised label
# ("Montaż" for "IC Mounting") no longer names any ShelfOS parameter definition and
# is silently dropped, exactly the trap TME_LANGUAGE above is pinned against.
FARNELL_API_KEY = os.environ.get("SHELFOS_FARNELL_API_KEY", "").strip()
FARNELL_STORE = os.environ.get("SHELFOS_FARNELL_STORE", "uk.farnell.com").strip()

# A visible field separator some barcode scanners emit in place of the ISO 15434
# group separator (GS, 0x1D). The scan parser always accepts GS/RS; set this if your
# scanner is configured to send a printable one (e.g. "|") instead.
SCAN_SEPARATOR = os.environ.get("SHELFOS_SCAN_SEPARATOR", "").strip()

# --- Label printer (spec §7) -------------------------------------------------
# The tape in the printer, as a brother_ql identifier ("62" = 62 mm continuous,
# "62x29" = die-cut, "29", "29x90", "12", …). The default is the continuous
# 62 mm roll a QL-800 ships with: no die to align to, and the label's length is
# ours to choose. A tape is a fact about the deployment, not about a request, so
# it is configured once here rather than passed per print.
LABEL_TAPE = os.environ.get("SHELFOS_LABEL_TAPE", "62").strip()

# The tapes actually owned, as a comma-separated list of the same identifiers —
# what the printing dialog offers to choose between. brother_ql knows two dozen
# tapes and nobody stocks them all, so naming yours makes the picker a short
# list of real rolls. Empty offers every tape a readable label fits on.
LABEL_TAPES = os.environ.get("SHELFOS_LABEL_TAPES", "").strip()

# How long each label is, in mm, on a CONTINUOUS tape (a die-cut label's length
# is fixed by the die, and this is ignored for one). 30 mm fits a QR that a
# phone reads at arm's length plus three lines of path.
LABEL_LENGTH_MM = float(os.environ.get("SHELFOS_LABEL_LENGTH_MM", "30"))

# White border kept clear on every side. Thermal tape is never fed perfectly
# straight, and ink at the very edge of a QR is what makes it unreadable.
LABEL_MARGIN_MM = float(os.environ.get("SHELFOS_LABEL_MARGIN_MM", "2"))

# TrueType files for the label text. Empty means "find one" — the renderer walks
# a list of paths common on Linux (DejaVu, Liberation, Noto). Set these when the
# host has none of those, or to print a font of your own choosing.
LABEL_FONT = os.environ.get("SHELFOS_LABEL_FONT", "").strip()
LABEL_FONT_BOLD = os.environ.get("SHELFOS_LABEL_FONT_BOLD", "").strip()

# Where the label printer is. Either a device this host can write to — a Brother
# QL on USB is /dev/usb/lp0 — or "tcp://host:port" for one reached through a
# bridge on the machine it is plugged into, which is how a server prints to a
# printer sitting on somebody's desk (see README). Empty (the default) means no
# printer: labels can still be previewed and printed through the browser, and
# the print buttons stay hidden. The raster bytes go to this device directly;
# ShelfOS does not go through CUPS, and CUPS holding the same printer will make
# writes fail either way.
LABEL_DEVICE = os.environ.get("SHELFOS_LABEL_DEVICE", "").strip()

# The account on THIS machine that a computer with a label printer logs in as to
# carry it here over ssh (see "./shelfos.sh tunnel-key"). Read for one purpose:
# filling it into the form on /label-printer, so nobody has to be told the name.
# Empty (the default, and what a deploy that predates the account leaves) means
# the field starts blank and is typed by hand.
TUNNEL_USER = os.environ.get("SHELFOS_TUNNEL_USER", "").strip()

# The file sshd is told to read the tunnel account's keys out of (through the
# AuthorizedKeysCommand a deploy installs). ShelfOS owns it, which is what lets
# a printer register itself without anybody logging in to this machine: the
# service writes an ordinary file, and no part of ShelfOS needs privileges.
# Empty (the default) means this server was not set up for it, and the page then
# says how to authorise a key by hand instead of offering a button that cannot
# work.
TUNNEL_KEYS_FILE = os.environ.get("SHELFOS_TUNNEL_KEYS", "").strip()

# How long the registration token inside a downloaded setup script is good for.
# Long enough to download it today and run it at the weekend; short enough that
# a forgotten copy in Downloads stops being a way in. It authorises one thing —
# adding a key that may bind one loopback port — and never a sign-in.
TUNNEL_ENROLL_HOURS = int(os.environ.get("SHELFOS_TUNNEL_ENROLL_HOURS", "168"))

# Which Brother QL is on the other end. The 800 series has its own raster
# header, so a wrong model here produces a printer that takes the job and does
# nothing with it.
LABEL_PRINTER_MODEL = os.environ.get("SHELFOS_LABEL_PRINTER_MODEL", "QL-800").strip()

# Ceiling on one print job. Deliberately far below the 500-label cap on building
# labels: that many is half a roll fed out by one mis-click, and unlike a
# mis-typed query it cannot be undone by reloading the page.
LABEL_MAX_JOB = int(os.environ.get("SHELFOS_LABEL_MAX_JOB", "50"))

# How long a print may wait for the printer to be free. There is exactly one
# printer, so jobs are serialised; a second click during a six-label job should
# wait its turn, not be refused, but not hold a worker thread indefinitely.
LABEL_PRINT_TIMEOUT = float(os.environ.get("SHELFOS_LABEL_PRINT_TIMEOUT", "30"))

# How long to wait for the printer to answer a status question. A QL answers in
# milliseconds; this is only the ceiling for a device that says nothing at all,
# after which ShelfOS prints unchecked rather than refusing to print.
LABEL_STATUS_TIMEOUT = float(os.environ.get("SHELFOS_LABEL_STATUS_TIMEOUT", "2"))


def label_printing_configured() -> bool:
    """True when this SETTING names a printer.

    Not the same question as "can this ShelfOS print", which a registered tunnel
    can also answer — see :func:`app.services.label_printer.printing_configured`,
    which is what the pages ask. This one stays because the startup checks are
    about the setting itself: what it names, and whether it can be opened.
    """
    return bool(LABEL_DEVICE)
