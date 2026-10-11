"""A component's photo and datasheet, from the shop it came from or another one.

A shop import names an image and a datasheet, but not every shop lets a server
download them: Mouser's Akamai answers every script — the hosted server and a
laptop alike — with an HTML block page, and TME's document host with a
Cloudflare challenge; ``url_fetch`` refuses both. So a part bought there used to
arrive without a picture, and with its datasheet only as a link. The same part
is usually sold by Digi-Key or element14 too, whose APIs look a part up by its
MPN and whose files download (Digi-Key's datasheet is often the maker's own
PDF), so when the import's file cannot be had, theirs is used instead.

A fallback answer must be THIS part. Its MPN has to match the component's, and
both sides have to name a maker that :func:`manufacturer_service.conflicts_with`
finds no conflict between: one number is sold under several makers, even within
a single shop — element14 answers a Keystone "5270" with ABB's. A component that
names no maker has nothing to tell those apart by, so it gets only the file it
was given.

Each search shares one time budget, the single download's own ceiling, so a
dialog waiting on it waits no longer than it did for one download (plus at most
one shop lookup already under way).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import cast

from sqlmodel import Session

from app import config
from app.models.attachment import Attachment
from app.models.component import Component
from app.models.enums import AttachmentKind
from app.services import attachment_service, shops, url_fetch
from app.services import manufacturer_service as ms
from app.services._common import normalize, refuse_if_deleted, require_entity
from app.services.errors import ValidationError
from app.services.shops.base import ProductData, ShopLookupMiss

# Shops asked by part number once the imported file has failed, in order. Only
# shops whose API takes an MPN: TME's takes its own catalogue symbol instead.
FALLBACK_SHOPS = ("digikey", "farnell")

# The two files a shop answer names, by attachment kind: which field holds the
# URL, and what to call it in a sentence.
_FILES: dict[AttachmentKind, tuple[str, str]] = {
    AttachmentKind.PHOTO: ("image_url", "image"),
    AttachmentKind.DATASHEET: ("datasheet_url", "datasheet"),
}

# A shop that could not answer at all — not configured, unreachable, an
# unreadable answer, a refusal with a failing status (Mouser's spent quota) — is
# not asked again for this long, by any request. The invoice page asks once per
# line, one request each; without this a 12-line invoice with TME down would wait
# out 12 TME timeouts. A miss, or a refusal of one part (Digi-Key's "Duplicate
# Products found"), says nothing about the next part, and trips nothing.
SHOP_DOWN_SECONDS = 120.0
_UNAVAILABLE = re.compile(
    r"integration is not configured|could not reach |could not read the "
    r"|\(HTTP \d+\)",
    re.IGNORECASE,
)
_down: dict[str, tuple[float, str]] = {}
_down_lock = threading.Lock()

# One request at a time per component AND kind of file, so two that race (an
# invoice finalized in two tabs, a retry while the first is still downloading)
# cannot both find none and both attach one: the second waits, then finds the
# first's. Per kind, so the import dialog's photo and datasheet — fetched side by
# side on purpose — do not queue behind each other. Per process, which is what
# ShelfOS runs as.
_locks: dict[tuple[int, AttachmentKind], threading.Lock] = {}
_locks_guard = threading.Lock()

#: Fallback shops' answers within one request, by shop: a photo and a datasheet
#: wanted together cost each shop one lookup, not two.
Answers = dict[str, tuple[ProductData | None, str]]


@contextmanager
def one_at_a_time(component_id: int, *kinds: AttachmentKind) -> Iterator[None]:
    """Hold the component's turn for these kinds of file (in a fixed order)."""
    with _locks_guard:
        locks = [
            _locks.setdefault((component_id, kind), threading.Lock())
            for kind in sorted(set(kinds), key=lambda k: k.value)
        ]
    with ExitStack() as held:
        for lock in locks:
            held.enter_context(lock)
        yield


def ask(
    shop: str, clock: Callable[[], float], call: Callable[[], ProductData]
) -> ProductData:
    """``call()`` against ``shop``, unless that shop was found down a moment ago.

    Every lookup this module and the invoice fill-in make goes through here, so
    a shop found down by one is skipped by all (see ``SHOP_DOWN_SECONDS``).
    """
    with _down_lock:
        until, reason = _down.get(shop, (0.0, ""))
    if clock() < until:
        raise ValidationError(f"{reason} (not asked again for a while)")
    try:
        product = call()
    except ShopLookupMiss:
        raise
    except ValidationError as exc:
        if _UNAVAILABLE.search(str(exc)):
            with _down_lock:
                _down[shop] = (clock() + SHOP_DOWN_SECONDS, str(exc))
        raise
    with _down_lock:
        _down.pop(shop, None)
    return product


def attach_from_shops(
    session: Session,
    component_id: int,
    kind: AttachmentKind,
    url: str | None,
    *,
    source_url: str | None = None,
    source_shop: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Attachment, bool]:
    """Give the component a photo or a datasheet; ``(attachment, created)``.

    ``url`` is what the import's own shop offered, tried first; it may be
    absent, since a shop with no file for a part may sell it all the same. Then
    each fallback shop is asked by the component's MPN — except the one
    ``source_url`` came from, which would only offer the same file again — and
    its file used if the answer is this part. ``source_shop`` names that shop by
    its key instead, for a caller that has no URL for it.

    Requests for one component and kind take turns (:func:`one_at_a_time`). One
    that already has an attachment of this kind gets it back with ``created``
    False, and nothing is fetched: a retried or doubled request must not stack
    copies.

    Raises ValidationError, naming what each source said, when nothing could be
    had — and before any shop is asked, for a deleted component.
    """
    component = require_entity(session, Component, component_id, "component")
    refuse_if_deleted(component, component.mpn or f"component #{component_id}")
    with one_at_a_time(component_id, kind):
        existing = next(
            (
                a
                for a in attachment_service.list_attachments(
                    session, entity_type="component", entity_id=component_id
                )
                if a.kind is kind
            ),
            None,
        )
        if existing is not None:
            return existing, False
        attached = attach_from_shops_held(
            session,
            component,
            kind,
            url,
            source=source_shop or shops.shop_key(source_url),
            deadline=clock() + config.ATTACHMENT_URL_TOTAL_TIMEOUT,
            clock=clock,
        )
        return attached, True


def attach_from_shops_held(
    session: Session,
    component: Component,
    kind: AttachmentKind,
    url: str | None,
    *,
    source: str | None,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    answers: Answers | None = None,
    tried: list[str] | None = None,
) -> Attachment:
    """:func:`attach_from_shops`'s search, for a caller already holding the turn.

    For a caller that knows the component has no such file and holds
    :func:`one_at_a_time` itself, so neither is repeated. ``deadline`` is the
    caller's, shared with whatever else it fetches for this component;
    ``answers`` lets two searches in one request share the fallback shops'
    answers; ``tried`` names URLs the caller already failed to download.
    """
    component_id = cast(int, component.id)
    field_name, noun = _FILES[kind]
    tried = list(tried or [])
    answers = {} if answers is None else answers
    reasons: list[str] = []

    def attempt(url: str, source: str) -> Attachment | None:
        tried.append(url)
        try:
            data, filename = url_fetch.fetch_url(url, deadline=deadline)
            # Inside the try too: an empty or oversize file is this source's
            # failure, and the next one may still have a usable one.
            return attachment_service.create_attachment(
                session,
                entity_type="component",
                entity_id=component_id,
                kind=kind,
                filename=filename,
                data=data,
            )
        except ValidationError as exc:
            reasons.append(f"{source}: {exc}")
            return None

    if url and url not in tried:
        attached = attempt(url, f"the shop's {noun}")
        if attached is not None:
            return attached

    if not (component.mpn and component.manufacturer):
        what = "part number" if not component.mpn else "manufacturer"
        reasons.append(f"no {what} to check another shop's answer against")
    else:
        for shop in FALLBACK_SHOPS:
            if shop == source:
                continue
            # A lookup is not cut short by the deadline, so one is started only
            # while it can finish inside it — unless its answer is already in.
            if shop not in answers and deadline - clock() < config.SHOP_API_TIMEOUT:
                reasons.append(f"{shop}: not asked, out of time")
                continue
            product, reason = fallback_product(session, component, shop, clock, answers)
            if product is None:
                reasons.append(f"{shop}: {reason}")
                continue
            found = getattr(product, field_name)
            if not found:
                reasons.append(f"{shop}: no {noun} for this part")
                continue
            if found in tried:  # the same file, refused once already
                continue
            attached = attempt(found, shop)
            if attached is not None:
                return attached

    raise ValidationError(
        f"no {kind.value} could be downloaded — " + "; ".join(reasons)
    )


def fallback_product(
    session: Session,
    component: Component,
    shop: str,
    clock: Callable[[], float],
    answers: Answers,
) -> tuple[ProductData | None, str]:
    """``(product, "")`` when ``shop`` sells this very part, else ``(None, why)``.

    Asked once per request (``answers``), however many files are wanted from it.
    """
    if shop not in answers:
        answers[shop] = _check_fallback(session, component, shop, clock)
    return answers[shop]


def _check_fallback(
    session: Session, component: Component, shop: str, clock: Callable[[], float]
) -> tuple[ProductData | None, str]:
    try:
        product = ask(
            shop,
            clock,
            lambda: shops.import_by_mpn(
                shop, component.mpn or "", manufacturer=component.manufacturer
            ),
        )
    except ValidationError as exc:
        # Not configured, a miss, the API down: this shop simply has nothing to
        # offer, and the next one may.
        return None, str(exc)
    if normalize(product.mpn) != normalize(component.mpn):
        return None, f"answered with {product.mpn or 'an unnamed part'}"
    # False, not merely "not True": a shop answer naming no maker cannot show
    # it is not another company's part.
    if (
        ms.conflicts_with(
            ms.canonical_name(session, product.manufacturer),
            ms.canonical_name(session, component.manufacturer),
        )
        is not False
    ):
        return None, f"answered with {product.manufacturer or 'an unnamed'}'s part"
    return product, ""
