"""Parse a scanned barcode/QR into a shop + identifier (spec: scan to prefill).

Two shapes reach us, both as plain text a keyboard-wedge scanner typed into a field:

* **A QR that embeds a product URL** (TME) or a plain pasted shop URL — the URL is a
  contiguous token, so it survives regardless of how the scanner treats separators.
* **A DataMatrix** (Mouser, Digi-Key) in ISO 15434 / ANSI MH10.8.2: a ``[)>`` envelope
  and fields separated by the group/record separator, each starting with a Data
  Identifier (``1P``=MPN, ``30P``=distributor PN, ``1V``=manufacturer, …).

The DataMatrix is only parseable when the field separators are present. Many scanners
emit the separator as a *key* (e.g. F-key), which never reaches an ``<input>`` value,
so the fields concatenate and their boundaries become genuinely ambiguous — we refuse
to guess and say so plainly. Configure the scanner to keep the separators (GS ``0x1D``,
or a visible one via ``SHELFOS_SCAN_SEPARATOR``) for DataMatrix support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import config
from app.services.errors import ValidationError

# A token runs to whitespace or a control character (a separator may sit flush
# against the end of a URL), and trailing punctuation is sentence noise, not part
# of the value — it would otherwise be stored as part of the shop link.
_URL = re.compile(r"https?://[^\s\x00-\x20]+")
_TME_PN = re.compile(r"\bPN:([^\s\x00-\x20]+)")
_TRAILING = ".,;:!?'\"()[]{}<>"
# The Digi-Key-only Z data identifiers — a strong signal it's a Digi-Key label.
_DIGIKEY_Z = ("11Z", "12Z", "13Z", "20Z")

_NO_SEPARATORS = (
    "This scanner isn't sending field separators, so the barcode can't be read. Scan a "
    "TME QR or paste a shop URL — or use a scanner that keeps the separators."
)


@dataclass
class ScanResult:
    """What a scan yields: a URL to look up, and/or an MPN + manufacturer + shop."""

    url: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    shop: str | None = None  # "mouser" | "digikey" | None


def parse_scan(code: str) -> ScanResult:
    """Parse scanned text into a :class:`ScanResult`, or raise ``ValidationError``."""
    text = code.strip()
    if not text:
        raise ValidationError("nothing to import")
    # The ISO 15434 envelope is unambiguous, so a DataMatrix is detected first.
    if "[)>" in text:
        return _parse_datamatrix(text)
    # Otherwise a URL: a TME QR embeds a product URL (with a PN: token we keep as a
    # fallback), or the user pasted a shop URL directly.
    url_match = _URL.search(text)
    pn_match = _TME_PN.search(text)
    if url_match or pn_match:
        return ScanResult(
            url=url_match.group(0).rstrip(_TRAILING) if url_match else None,
            mpn=pn_match.group(1).rstrip(_TRAILING) if pn_match else None,
        )
    raise ValidationError("unrecognised code — expected a shop URL or a barcode")


def _configured_separator() -> str | None:
    """The visible separator from the environment, if it is safe to split on.

    A separator that can occur *inside* a field would shred a real label into
    confidently-wrong values ("-" turns 1PESQ-106-33-T-S into three fields), so an
    unusable setting is ignored rather than trusted — the GS/RS split still works.
    """
    separator = config.SCAN_SEPARATOR
    if len(separator) != 1 or separator.isalnum() or separator in "-._/+":
        return None
    return separator


def _parse_datamatrix(text: str) -> ScanResult:
    separators = ["\x1d", "\x1e"]  # GS, RS
    configured = _configured_separator()
    if configured:
        separators.append(configured)
    pattern = "|".join(re.escape(s) for s in separators)
    fields = [f for f in re.split(pattern, text) if f]

    mpn: str | None = None
    manufacturer: str | None = None
    distributor_pn: str | None = None
    has_digikey_z = False
    for field in fields:
        if field.startswith("30P"):
            distributor_pn = field[3:].strip()
        elif field.startswith("1P"):
            mpn = field[2:].strip()
        elif field.startswith("1V"):
            manufacturer = field[2:].strip()
        elif field[:3] in _DIGIKEY_Z:
            has_digikey_z = True

    # Not "did the split produce more than one field" — GS and RS are separately
    # configurable on most scanners, so one that drops GS but keeps the RS inside
    # the header yields two fields with the body still concatenated. What proves the
    # payload really was split is finding a data identifier we recognise.
    if not (mpn or manufacturer or distributor_pn):
        raise ValidationError(_NO_SEPARATORS)

    # Mouser prints nothing uniquely its own, so it's the default. That is safe
    # rather than a guess: 1P is a MANUFACTURER part number, so looking it up at
    # Mouser is meaningful whoever printed the label — and a shop that doesn't
    # carry it just answers "no product found", which falls back to the label.
    is_digikey = (
        distributor_pn is not None and distributor_pn.upper().endswith("-ND")
    ) or has_digikey_z
    return ScanResult(
        url=None,
        mpn=mpn or None,
        manufacturer=manufacturer or None,
        shop="digikey" if is_digikey else "mouser",
    )
