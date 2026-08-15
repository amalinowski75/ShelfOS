"""Parse a scanned barcode/QR into a shop + identifier (spec: scan to prefill).

Two shapes reach us, both as plain text a keyboard-wedge scanner typed into a field:

* **A QR that embeds a product URL** (TME) or a plain pasted shop URL — the URL is a
  contiguous token, so it survives regardless of how the scanner treats separators.
* **A DataMatrix** (Mouser, Digi-Key) in ISO 15434 / ANSI MH10.8.2: a ``[)>`` envelope
  and fields separated by the group/record separator, each starting with a Data
  Identifier (``1P``=MPN, ``30P``=distributor PN, ``1V``=manufacturer, …).

The DataMatrix is only parseable when the field separators survive the trip. Three
things a scanner does with them, and what happens here:

* **Sends GS/RS as characters** — parsed directly, nothing to work out.
* **Prints a visible stand-in** (this user's scanner sends ``|``) — detected
  automatically, but only on evidence: a candidate is accepted when splitting on it
  yields a Data Identifier we know. ``SHELFOS_SCAN_SEPARATOR`` still declares one
  outright for anything the candidate list doesn't cover.
* **Emits the separator as a *key*** (e.g. an F-key), which never reaches an
  ``<input>`` value — the fields concatenate and their boundaries become genuinely
  ambiguous, so we refuse to guess and say so plainly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import config
from app.services.errors import ValidationError

# A token runs to whitespace or a control character (a separator may sit flush
# against the end of a URL), and trailing punctuation is sentence noise, not part
# of the value — it would otherwise be stored as part of the shop link.
#
# Case-insensitive: QR alphanumeric mode can only encode upper case, so plenty of
# encoders emit HTTPS://WWW.TME.EU/… — and the providers' matches() lower-cases the
# host anyway, so a case-sensitive parse here would reject a URL the registry can
# resolve perfectly well.
_URL = re.compile(r"https?://[^\s\x00-\x20]+", re.IGNORECASE)
_TME_PN = re.compile(r"\bPN:([^\s\x00-\x20]+)", re.IGNORECASE)
# The manufacturer's own part number, printed beside the shop's. ``\b`` before
# "PN:" does not match inside "MPN:", so the two tokens never collide.
_TME_MPN = re.compile(r"\bMPN:([^\s\x00-\x20]+)", re.IGNORECASE)
_TRAILING = ".,;:!?'\"()[]{}<>"
# The Digi-Key-only Z data identifiers — a strong signal it's a Digi-Key label.
_DIGIKEY_Z = ("11Z", "12Z", "13Z", "20Z")

# Visible stand-ins a scanner may print in place of the GS/RS control characters,
# tried automatically when the control characters are absent. Deliberately tiny:
# every one of these is a character that does not occur inside a manufacturer or
# distributor part number, so a split on one can be trusted once it yields a
# known data identifier. Anything that CAN occur inside a value (- . _ / +) is
# excluded here for the same reason configured_separator() rejects it.
_AUTO_SEPARATORS = ("|", "^", "~", "¦")

# Reached only after the visible stand-ins above have been tried and found nothing,
# so the remaining causes are a scanner sending no separator at all (it emits one as
# a key press, which never reaches an input) or a label carrying only identifiers we
# don't read. The advice names what is still left to do, not what the parser already
# did by itself.
_UNREADABLE = (
    "No readable fields in this barcode — either your scanner is sending no field "
    "separator at all (not even a printable one), or this label isn't one ShelfOS "
    "can read. Scan a TME QR or paste a shop URL, or set the scanner to send GS "
    "(0x1D) or a printable separator such as '|'."
)


@dataclass
class ScanResult:
    """What a scan yields: a URL to look up, and/or an MPN + manufacturer + shop."""

    url: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    shop: str | None = None  # "mouser" | "digikey" | None
    # The 30P field: the DISTRIBUTOR's own part number (Mouser No, Digi-Key
    # "…-ND"). Matches an invoice line's supplier_part_number, which is exactly
    # what a bag scanned against a draft invoice needs to be matched by.
    distributor_pn: str | None = None
    # The MANUFACTURER's part number where the label states it separately (a TME
    # QR's ``MPN:`` token; on a DataMatrix the 1P field already lands in ``mpn``).
    # This is what matches a stored component, whose ``mpn`` is the
    # manufacturer's — a TME bag's ``PN:`` is TME's own symbol and often differs.
    manufacturer_pn: str | None = None


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
    mpn_match = _TME_MPN.search(text)
    if url_match or pn_match or mpn_match:
        return ScanResult(
            url=url_match.group(0).rstrip(_TRAILING) if url_match else None,
            mpn=pn_match.group(1).rstrip(_TRAILING) if pn_match else None,
            manufacturer_pn=(
                mpn_match.group(1).rstrip(_TRAILING) if mpn_match else None
            ),
        )
    raise ValidationError("unrecognised code — expected a shop URL or a barcode")


def configured_separator() -> str | None:
    """The visible separator from the environment, if it is safe to split on.

    A separator that can occur *inside* a field would shred a real label into
    confidently-wrong values ("-" turns 1PESQ-106-33-T-S into three fields), so an
    unusable setting is ignored rather than trusted — the GS/RS split still works.

    Public so startup can warn about a setting that is being ignored; silently
    doing nothing is indistinguishable from the feature being broken.
    """
    separator = config.SCAN_SEPARATOR
    if len(separator) != 1 or separator.isalnum() or separator in "-._/+":
        return None
    return separator


def _split_fields(text: str, separators: list[str]) -> list[str]:
    pattern = "|".join(re.escape(s) for s in separators)
    return [f for f in re.split(pattern, text) if f]


def _read_fields(fields: list[str]) -> tuple[str | None, str | None, str | None, bool]:
    """Pull the identifiers we understand out of already-split fields."""
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
    return mpn, manufacturer, distributor_pn, has_digikey_z


def _parse_datamatrix(text: str) -> ScanResult:
    separators = ["\x1d", "\x1e"]  # GS, RS
    configured = configured_separator()
    if configured:
        separators.append(configured)
    mpn, manufacturer, distributor_pn, has_digikey_z = _read_fields(
        _split_fields(text, separators)
    )

    # Not "did the split produce more than one field" — GS and RS are separately
    # configurable on most scanners, so one that drops GS but keeps the RS inside
    # the header yields two fields with the body still concatenated. What proves the
    # payload really was split is finding a data identifier we recognise. That does
    # conflate the dropped-separator case with a label carrying only identifiers we
    # don't read, hence the message names both rather than misdiagnosing the scanner.
    if not (mpn or manufacturer or distributor_pn):
        # Many scanners can't emit the ISO control characters at all and are
        # configured to print a visible stand-in. Rather than make the user
        # declare it (SHELFOS_SCAN_SEPARATOR), try the few characters that can
        # play that role, and accept one only on EVIDENCE: the split has to
        # yield a data identifier we know. A candidate that occurs inside real
        # values is not offered here — splitting "1PESQ-106-33-T-S" on "-"
        # would leave "1PESQ" and look convincingly like a success.
        best: tuple[str | None, str | None, str | None, bool] | None = None
        best_score = 0
        for candidate in _AUTO_SEPARATORS:
            if candidate not in text:
                continue
            found = _read_fields(_split_fields(text, [*separators, candidate]))
            # Score, not first-past-the-post: a real separator splits the WHOLE
            # label, so it yields more identifiers than a stray character that
            # happens to sit in front of one. (A "|" inside a value on a
            # "^"-separated label would otherwise win and lose the rest.)
            score = sum(1 for value in found[:3] if value)
            if score > best_score:
                best, best_score = found, score
        if best is None:
            raise ValidationError(_UNREADABLE)
        mpn, manufacturer, distributor_pn, has_digikey_z = best

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
        distributor_pn=distributor_pn or None,
    )
