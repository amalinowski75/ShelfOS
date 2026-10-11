#!/usr/bin/env python3
"""Give every component without a photo one from the shop its link points at.

Works entirely through a running ShelfOS's HTTP API — it never opens the
database — so it goes through the same validation, SSRF guard and audit trail as
the web UI, and works against a remote server just as well as a local one.

For each component with no photo attachment it walks the component's links
(``shop`` first, then ``product`` and ``other``; datasheet links are skipped),
asks the server to look the part up at that shop, and hands what it found to
``POST /api/components/{id}/photo-from-shops`` — the endpoint the import dialog
uses. The server downloads that shop's image, or, when there is none or the shop
will not let it (Mouser's Akamai refuses every script), Digi-Key's or element14's
picture of the same part, checked by part number and maker. The fallback lives
there alone, so the dialog and this script cannot drift apart.

**The shop's MPN must match the component's, and its manufacturer must not
plainly be another company** before anything is attached — one number is sold
under several makers, even within a single shop (Farnell answers a Keystone
"5270" with ABB's). The makers are compared by the server
(``/api/manufacturers/same-mpn``): an alias, or a shop's longer spelling of the
same name ("NXP USA Inc." for "NXP Semiconductors"), is no conflict, and neither
is a side that names nobody. The server must be a build that answers it; the
script stops if it is not. A component with no MPN cannot be checked and
is skipped. How the shop is asked
depends on the shop:

* **TME** is asked through the link itself: its product URL carries TME's own
  symbol, the only key its API resolves (a part number would not find it).
* **Every other shop** is asked by part number first (the component's MPN, with
  its manufacturer as the tiebreaker) — the lookup the invoice import makes. The
  link an invoice import stores for these shops is a keyword SEARCH, and looking
  one up would spend a call on whatever the shop makes of the path. Only when
  the part number misses is the link itself tried, which still finds a part
  whose link is a real product page.

The linked shop's own answer is checked here, more leniently than a fallback's:
a maker named on one side only does not refuse it. That shop is where the part
was bought, so an MPN match there is strong evidence; another shop's same number
is not, and the server asks it for a maker on both sides.

``--dry-run`` looks the linked shop up but asks the server for nothing, so it
cannot tell whether a fallback shop would have had a picture.

A component that already has a photo attachment the component table does not
count (one stored under a name with no image extension) is skipped too, so a
second run never attaches the same picture again.

It does what it can and reports the rest: a component with no shop link, or one
no shop had a picture of that the server could download, is listed in the
summary with each source's reason. One component's failure never stops the run;
the exit status is 1 when a shop could not answer at all (not configured,
unreachable, an unreadable response, out of time) or an API call failed, and 0
when what was left undone was only what the data and the shops allow.

Usage::

    python scripts/fetch_missing_photos.py --url http://localhost:9000 --user admin
    python scripts/fetch_missing_photos.py --dry-run     # look up, attach nothing
    python scripts/fetch_missing_photos.py --only 12 --only 40

The password is read from ``SHELFOS_PASSWORD`` or asked for. The account must be
allowed to write (attaching a file is a writer's action), and every lookup spends
the shop's API quota, so lookups are paced: ``--delay`` apart for each shop, and
never faster than Mouser's 30 calls a minute (faster, and it answers "could not
read the Mouser response" for whole runs of parts). A lookup that still meets
that answer is retried once, half a minute later.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_URL = "http://localhost:9000"

# Seconds between two lookups at one shop, where its API sets a rate limit.
MIN_INTERVAL = {"mouser": 2.1}

# A rate-limited (or briefly failing) shop API surfaces as the provider's
# "could not read the <shop> response", unlike a plain miss; such a lookup is
# retried once after this many seconds. Pacing alone still met it in runs of a
# dozen parts — another client spending the same key counts against it too.
RETRY_AFTER = 30.0

# The order links are tried in; datasheet links never lead to a product page.
LINK_KINDS = ("shop", "product", "other")

# Host label → the shop key ``/api/shops/lookup`` takes for an index lookup.
# Matched as a domain label, the top-level one excluded, so country domains
# (mouser.pl, digikey.co.uk) count. This mirrors each provider's ``matches()`` in
# app/services/shops/ and the keys of its ``_BY_INDEX``: a distributor added
# there needs an entry here too, or its parts land under "no shop link".
SHOP_HOSTS = {
    "mouser": "mouser",
    "digikey": "digikey",
    "tme": "tme",
    "farnell": "farnell",
    "element14": "farnell",
    "newark": "farnell",
}


def shop_key(url: str) -> str | None:
    """The shop a URL belongs to, or None for a host no provider serves."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:  # e.g. an unbalanced-bracket IPv6 literal
        return None
    for label in host.split(".")[:-1]:
        if label in SHOP_HOSTS:
            return SHOP_HOSTS[label]
    return None


def mismatch(api: Api, row: dict[str, Any], product: dict[str, Any]) -> str | None:
    """Why a shop's answer is not this component's part, or None if it is.

    The MPN has to match, and the maker must not contradict the component's: one
    number is sold under several makers, even within a single shop. A side that
    names no maker leaves the MPN to decide, as a scan does.
    """
    if not same_mpn(product.get("mpn"), row.get("mpn")):
        return f"answered with {product.get('mpn') or 'an unnamed part'}"
    maker = product.get("manufacturer")
    # Either side silent: no verdict to ask for, so no round trip either.
    if maker and row.get("manufacturer") and api.maker_conflicts(row, maker):
        return f"answered with {maker}'s part, not {row.get('manufacturer')}'s"
    return None


# What the shop providers say when the SHOP could not answer at all — as opposed
# to answering that it does not know the part. Asking that shop again (through
# a link instead of a part number) would fail the same way, and the run did not
# do what it could, so it exits 1. Matched on the providers' messages in
# app/services/shops/, which the API passes through as the 422's detail.
_UNAVAILABLE = re.compile(
    r"integration is not configured|could not reach "
    r"|could not read the [\w-]+ (token )?response$|^HTTP 5\d\d|out of time$"
)


def unavailable(reason: str | None) -> bool:
    """Whether a failed lookup means the shop could not answer, not a miss."""
    return bool(reason and _UNAVAILABLE.search(reason))


@dataclass
class Search:
    """What looking one component up across the shops came to."""

    #: Lookups the shop could not answer at all, as "shop: reason".
    errors: list[str] = field(default_factory=list)


def same_mpn(a: str | None, b: str | None) -> bool:
    """Whether two MPNs name the same part, ignoring case and punctuation."""

    def norm(value: str | None) -> str:
        return re.sub(r"[^0-9a-z]", "", (value or "").casefold())

    return bool(norm(a)) and norm(a) == norm(b)


def error_text(resp: httpx.Response) -> str:
    """The server's own message for a failed call, else its status."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    # Anything may answer in front of ShelfOS (a reverse proxy, the wrong server),
    # so nothing about the body's shape is taken for granted.
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):  # a FastAPI validation error
        detail = "; ".join(
            str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail
        )
    return (
        f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"
    )


@dataclass
class Report:
    attached: list[str] = field(default_factory=list)
    would_attach: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    # Skips the data explains (no link, no image …) are not failures; these are.
    failures: int = 0

    def skip(self, reason: str, label: str, *, failed: bool = False) -> None:
        self.skipped.setdefault(reason, []).append(label)
        self.failures += failed


class Api:
    def __init__(self, base_url: str, delay: float) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=60)
        self.delay = delay
        self.last_call: dict[str, float] = {}

    def login(self, username: str, password: str) -> None:
        resp = self.client.post(
            "/api/auth/token", json={"username": username, "password": password}
        )
        if resp.status_code != 200:
            sys.exit(f"Sign-in failed — {error_text(resp)}")
        # A bearer token needs no CSRF token; that is only for the cookie path.
        token = resp.json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"

    def components_without_photo(self) -> list[dict[str, Any]]:
        resp = self.client.get("/web/api/components", params={"photos": 1})
        if resp.status_code != 200:
            sys.exit(f"Could not list components — {error_text(resp)}")
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            sys.exit("Could not list components — unexpected response shape")
        return [row for row in rows if row.get("photo_id") is None]

    def links(self, component_id: int) -> list[dict[str, Any]]:
        resp = self.client.get(
            "/api/links", params={"entity_type": "component", "entity_id": component_id}
        )
        resp.raise_for_status()
        links = resp.json()
        return links if isinstance(links, list) else []

    def maker_conflicts(
        self, row: dict[str, Any], manufacturer: str | None
    ) -> bool | None:
        """Whether a shop's maker is plainly not the component's, by the server.

        Asked of the server rather than compared here: it resolves the alias
        table, folds names with a hand-written table, and accepts a shop's longer
        spelling of the same name ("NXP USA Inc." for "NXP Semiconductors") —
        rules a copy in this script would drift from. None when either side
        names nobody: silence contradicts nothing.
        """
        resp = self.client.get(
            "/api/manufacturers/same-mpn",
            params={"mpn": row["mpn"], "manufacturer": manufacturer},
        )
        resp.raise_for_status()
        body = resp.json()
        candidates = body.get("candidates") if isinstance(body, dict) else None
        for candidate in candidates if isinstance(candidates, list) else []:
            if isinstance(candidate, dict) and candidate.get("id") == row["id"]:
                if "manufacturer_conflicts" not in candidate:
                    # An older server: without the verdict every maker would pass
                    # unchecked, which is the very thing this guards against.
                    sys.exit(
                        "The server does not compare manufacturers yet — restart "
                        "it on a build whose /api/manufacturers/same-mpn answers "
                        "manufacturer_conflicts."
                    )
                verdict = candidate["manufacturer_conflicts"]
                return verdict if isinstance(verdict, bool) else None
        return None

    def has_photo(self, component_id: int) -> bool:
        """Whether the component has any attachment filed as a photo.

        The component table counts a photo only when its file is NAMED like an
        image, and a shop image URL without an extension is stored under such a
        name — so the table alone would send that part through every run again.
        """
        resp = self.client.get(
            "/api/attachments",
            params={"entity_type": "component", "entity_id": component_id},
        )
        resp.raise_for_status()
        attachments = resp.json()
        return isinstance(attachments, list) and any(
            isinstance(a, dict) and a.get("kind") == "photo" for a in attachments
        )

    def lookup(
        self, shop: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """A shop lookup: (product, None) or (None, the server's reason)."""
        product, reason = self._lookup_once(shop, body)
        if reason and re.search(r"could not read the [\w-]+ response$", reason):
            time.sleep(RETRY_AFTER)
            product, reason = self._lookup_once(shop, body)
        return product, reason

    def _lookup_once(
        self, shop: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        # Every call spends the shop's API quota, and some shops rate-limit it.
        interval = max(self.delay, MIN_INTERVAL.get(shop, 0.0))
        wait = self.last_call.get(shop, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last_call[shop] = time.monotonic()
        resp = self.client.post("/api/shops/lookup", json=body)
        if resp.status_code != 200:
            return None, error_text(resp)
        product = resp.json()
        if not isinstance(product, dict):
            return None, "unexpected response shape"
        return product, None

    def photo_from_shops(
        self, component_id: int, image_url: str | None, source_url: str | None
    ) -> list[str] | None:
        """Ask the server for a photo; None once one is attached, else the reasons.

        The reasons are what each source said, one per entry, as the server
        lists them ("digikey: no product found").
        """
        resp = self.client.post(
            f"/api/components/{component_id}/photo-from-shops",
            json={"image_url": image_url, "source_url": source_url},
        )
        if resp.status_code in (200, 201):
            return None
        if resp.status_code == 405 or (
            resp.status_code == 404 and error_text(resp) == "HTTP 404: Not Found"
        ):
            sys.exit(
                "The server has no /api/components/{id}/photo-from-shops yet — "
                "restart it on a build that does."
            )
        if resp.status_code != 422:
            resp.raise_for_status()
        detail = error_text(resp).removeprefix("HTTP 422: ")
        _, _, listed = detail.partition(" — ")
        return (listed or detail).split("; ")


def find_product(
    api: Api, row: dict[str, Any], links: list[dict[str, Any]], search: Search
) -> tuple[dict[str, Any] | None, str, str]:
    """The linked shop's record for this component, checked, or why there is none.

    Returns ``(product, shop key, "")`` on a match, else ``(None, "", reason)``.
    A shop that could not answer at all is noted in ``search.errors`` and not
    asked again through its other links.
    """
    mpn = row.get("mpn") or ""
    tried_shops: set[str] = set()
    down: set[str] = set()
    last_reason = "no shop link"

    def check(
        product: dict[str, Any] | None, reason: str | None, key: str, asked: str
    ) -> bool:
        nonlocal last_reason
        if product:
            wrong = mismatch(api, row, product)
            if wrong is None:
                return True
            reason = f"{asked} {wrong}"
        elif unavailable(reason):
            down.add(key)
            search.errors.append(f"{key}: {reason}")
        last_reason = reason or last_reason
        return False

    for link in links:
        url = link["url"]
        key = shop_key(url)
        if key is None or key in down:
            continue
        # By part number first, once per shop: a search link could not answer,
        # and a real product page names the same part anyway. Not for TME, whose
        # API keys on its own symbol — the one thing its link does carry.
        if key != "tme" and key not in tried_shops:
            tried_shops.add(key)
            product, reason = api.lookup(
                key,
                {
                    "shop_key": key,
                    "part_numbers": [mpn],
                    "manufacturer": row.get("manufacturer") or None,
                },
            )
            if check(product, reason, key, key):
                return product, key, ""
            if key in down:
                continue
        # The link as it stands: a product page the part number did not find.
        product, reason = api.lookup(key, {"code": url})
        if check(product, reason, key, "the link"):
            return product, key, ""
    return None, "", last_reason


def process(
    api: Api,
    row: dict[str, Any],
    label: str,
    report: Report,
    *,
    dry_run: bool,
) -> None:
    """Find and attach one component's photo, recording the outcome."""
    cid = row["id"]
    links = sorted(
        (link for link in api.links(cid) if link.get("kind") in LINK_KINDS),
        key=lambda link: LINK_KINDS.index(link["kind"]),
    )
    if not any(shop_key(link["url"]) for link in links):
        print("no shop link")
        report.skip("no shop link", label)
        return
    if not row.get("mpn"):
        print("no MPN to verify the shop's answer against")
        report.skip("no MPN", label)
        return
    if api.has_photo(cid):
        print("already has a photo (not named like an image)")
        report.skip("already has a photo the table does not show", label)
        return

    search = Search()
    product, shop, reason = find_product(api, row, links, search)
    image = product.get("image_url") if product else None
    # The linked shop, so the server does not ask it again as a fallback: it
    # would only offer the same picture, or the same miss.
    shop_links = [link["url"] for link in links if shop_key(link["url"])]
    source = next((u for u in shop_links if shop_key(u) == shop), shop_links[0])
    if dry_run:
        found = image or f"no image at the linked shop ({reason or 'none offered'})"
        print(f"would ask the server for a photo: {found}")
        report.would_attach.append(label)
        return
    reasons = api.photo_from_shops(row["id"], image, source)
    if reasons is None:
        print("photo attached")
        report.attached.append(label)
        return
    # A shop that could not answer at all might have had the picture: not done.
    trouble = search.errors + [r for r in reasons if unavailable(r.split(": ", 1)[-1])]
    print("no photo")
    report.skip(
        "no photo from any shop",
        f"{label} — {'; '.join(reasons)}",
        failed=bool(trouble),
    )


def run(api: Api, rows: list[dict[str, Any]], *, dry_run: bool) -> Report:
    report = Report()
    for n, row in enumerate(rows, 1):
        label = f"#{row['id']} {row.get('mpn') or '(no MPN)'}"
        print(f"[{n}/{len(rows)}] {label} … ", end="", flush=True)
        try:
            process(api, row, label, report, dry_run=dry_run)
        except httpx.HTTPError as exc:
            # A component deleted mid-run, a 5xx, a timeout: note it and go on,
            # so a long paced run still ends with its summary.
            reason = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else f"{type(exc).__name__}: {exc}"
            )
            print(f"error ({reason})")
            report.skip("API call failed", f"{label} — {reason}", failed=True)
    return report


def print_summary(report: Report, *, dry_run: bool) -> None:
    print()
    if dry_run:
        print(
            f"Would ask the server for a photo for {len(report.would_attach)} "
            "component(s)."
        )
    else:
        print(f"Attached a photo to {len(report.attached)} component(s).")
    for reason, labels in report.skipped.items():
        print(f"\n{reason}: {len(labels)}")
        for label in labels:
            print(f"  {label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"ShelfOS base URL (default {DEFAULT_URL})"
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("SHELFOS_USER", "admin"),
        help="account to sign in as (default admin, or SHELFOS_USER)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="look the parts up but attach nothing"
    )
    parser.add_argument(
        "--only",
        type=int,
        action="append",
        metavar="ID",
        help="only this component id (repeatable)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="seconds between shop lookups (default 0.5)",
    )
    args = parser.parse_args(argv)

    password = os.environ.get("SHELFOS_PASSWORD") or getpass.getpass(
        f"Password for {args.user}: "
    )
    api = Api(args.url, args.delay)
    api.login(args.user, password)

    rows = api.components_without_photo()
    if args.only:
        only = set(args.only)
        rows = [row for row in rows if row["id"] in only]
    print(f"{len(rows)} component(s) without a photo.\n")
    report = run(api, rows, dry_run=args.dry_run)
    print_summary(report, dry_run=args.dry_run)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
