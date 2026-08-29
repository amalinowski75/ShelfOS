"""Parse a scanned barcode/QR into a shop + identifier (spec: scan to prefill).

Two shapes reach us, both as plain text a keyboard-wedge scanner typed into a field:

* **A QR that embeds a product URL** (TME) or a plain pasted shop URL — the URL is a
  contiguous token, so it survives regardless of how the scanner treats separators.
* **A DataMatrix** (Mouser, Digi-Key, Farnell) in ISO 15434 / ANSI MH10.8.2: a ``[)>``
  envelope and fields separated by the group/record separator, each starting with a
  Data Identifier (``1P``=MPN, ``30P``/``3P``=the distributor's own number,
  ``1V``=manufacturer, …).

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
# Farnell prints its own order code under 3P, where Mouser and Digi-Key use 30P (or,
# on Mouser's labels, nothing at all). The data identifiers on the three real labels
# this repo holds as fixtures — recomputed from them, not recalled:
#
#   Mouser    11K 14K 1P 1V 4L K Q
#   Digi-Key  10K 11K 11Z 12Z 13Z 1K 1P 1T 20Z 30P 4L 9D K P Q
#   Farnell   1P 1T 3P 4K 4L 9D K P Q
#
# so 3P (and 4K, whose meaning is unknown and which is therefore not used) is the
# only field of Farnell's that neither of the others prints. Three labels is thin
# evidence for a rule, which is why it sits BELOW Digi-Key's multi-signal test and
# why Mouser stays the default: a shop that doesn't carry the part just answers
# "no product found" and the dialog fills from the label.
_FARNELL_ORDER_CODE = "3P"

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
    shop: str | None = None  # "mouser" | "digikey" | "farnell" | None
    # The DISTRIBUTOR's own part number — the 30P field (Mouser No, Digi-Key "…-ND")
    # or Farnell's 3P order code. Matches an invoice line's supplier_part_number,
    # which is exactly what a bag scanned against a draft invoice needs to be
    # matched by; for Farnell it is also the key its API resolves with `id:`.
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


@dataclass
class _Fields:
    """What one way of splitting a label yielded.

    A dataclass rather than a tuple because the members are of two kinds and the
    difference is load-bearing: ``identifiers`` counts only the VALUES, and a
    positional ``found[:3]`` doing that job is one careless edit away from letting a
    stray character that exposed a single flag outscore the real separator.
    """

    mpn: str | None = None
    manufacturer: str | None = None
    # 30P (Mouser, Digi-Key) and 3P (Farnell) both name the distributor's own
    # number. Held apart so the shop test can key on WHICH was printed, and so a
    # label carrying both resolves by rule instead of by whichever came last.
    pn_30p: str | None = None
    pn_3p: str | None = None
    has_digikey_z: bool = False

    @property
    def distributor_pn(self) -> str | None:
        # 30P wins: a label carrying both routes to Digi-Key on the stronger
        # evidence, so answering with Farnell's number alongside would be incoherent.
        return self.pn_30p or self.pn_3p

    @property
    def identifiers(self) -> int:
        """How many part identifiers this split found — the separator's evidence.

        Only the values. A flag says something about WHOSE label it is, not that the
        payload was split correctly, so counting one would let a stray character
        beat the separator that actually read the fields.
        """
        return sum(
            1 for value in (self.mpn, self.manufacturer, self.distributor_pn) if value
        )


def _read_fields(fields: list[str]) -> _Fields:
    """Pull the identifiers we understand out of already-split fields."""
    found = _Fields()
    for field in fields:
        # "30P" is tested before "3P" for the reader's sake only — the two prefixes
        # are disjoint ("30P…" does not start with "3P"), so neither can shadow the
        # other whatever the order.
        if field.startswith("30P"):
            found.pn_30p = field[3:].strip()
        elif field.startswith(_FARNELL_ORDER_CODE):
            found.pn_3p = field[2:].strip()
        elif field.startswith("1P"):
            found.mpn = field[2:].strip()
        elif field.startswith("1V"):
            found.manufacturer = field[2:].strip()
        elif field[:3] in _DIGIKEY_Z:
            found.has_digikey_z = True
    return found


def _parse_datamatrix(text: str) -> ScanResult:
    separators = ["\x1d", "\x1e"]  # GS, RS
    configured = configured_separator()
    if configured:
        separators.append(configured)
    found = _read_fields(_split_fields(text, separators))

    # Not "did the split produce more than one field" — GS and RS are separately
    # configurable on most scanners, so one that drops GS but keeps the RS inside
    # the header yields two fields with the body still concatenated. What proves the
    # payload really was split is finding a data identifier we recognise. That does
    # conflate the dropped-separator case with a label carrying only identifiers we
    # don't read, hence the message names both rather than misdiagnosing the scanner.
    if not found.identifiers:
        # Many scanners can't emit the ISO control characters at all and are
        # configured to print a visible stand-in. Rather than make the user
        # declare it (SHELFOS_SCAN_SEPARATOR), try the few characters that can
        # play that role, and accept one only on EVIDENCE: the split has to
        # yield a data identifier we know. A candidate that occurs inside real
        # values is not offered here — splitting "1PESQ-106-33-T-S" on "-"
        # would leave "1PESQ" and look convincingly like a success.
        best: _Fields | None = None
        best_score = 0
        for candidate in _AUTO_SEPARATORS:
            if candidate not in text:
                continue
            split = _read_fields(_split_fields(text, [*separators, candidate]))
            # Score, not first-past-the-post: a real separator splits the WHOLE
            # label, so it yields more identifiers than a stray character that
            # happens to sit in front of one. (A "|" inside a value on a
            # "^"-separated label would otherwise win and lose the rest.)
            if split.identifiers > best_score:
                best, best_score = split, split.identifiers
        if best is None:
            raise ValidationError(_UNREADABLE)
        found = best

    # Mouser prints nothing uniquely its own, so it stays the default. That is safe
    # rather than a guess: 1P is a MANUFACTURER part number, so looking it up at
    # Mouser is meaningful whoever printed the label — and a shop that doesn't
    # carry it just answers "no product found", which falls back to the label.
    #
    # Digi-Key is tested first because its evidence is the strongest: a "-ND" suffix
    # on its own part number, or one of four identifiers nobody else prints. Farnell
    # follows on the single 3P field.
    is_digikey = (
        found.distributor_pn is not None
        and found.distributor_pn.upper().endswith("-ND")
    ) or found.has_digikey_z
    if is_digikey:
        shop = "digikey"
    elif found.pn_3p is not None:
        shop = "farnell"
    else:
        shop = "mouser"
    return ScanResult(
        url=None,
        mpn=found.mpn or None,
        manufacturer=found.manufacturer or None,
        shop=shop,
        distributor_pn=found.distributor_pn or None,
    )
