#!/usr/bin/env python3
"""Give every component without a photo one from the shop its link points at.

Works entirely through a running ShelfOS's HTTP API — it never opens the
database — so it goes through the same validation, SSRF guard and audit trail as
the web UI, and works against a remote server just as well as a local one.

For each component with no photo attachment it walks the component's links
(``shop`` first, then ``product`` and ``other``; datasheet links are skipped),
asks the server to look the part up at that shop, and, when the shop names an
image, has the server download it as a ``photo`` attachment.

Two safeguards keep a wrong picture off a part:

* **The shop's MPN must match the component's.** Not every shop link names one
  product: for every shop but TME the link an invoice import stores is a keyword
  SEARCH, and looking one up answers for whatever the shop makes of the path. A
  component with no MPN cannot be checked and is skipped.
* When the link itself does not lead to the part, the shop is asked once more by
  its catalogue index (the component's MPN, with its manufacturer as the
  tiebreaker) — the same lookup the invoice import makes.

It does what it can and reports the rest: a component with no shop link, a shop
with no API key configured, a product with no image, or an image the shop will
not hand to a server (Mouser's Akamai refuses datasheet and photo downloads from
datacenter IPs) is listed in the summary, not treated as an error.

Usage::

    python scripts/fetch_missing_photos.py --url http://localhost:9000 --user admin
    python scripts/fetch_missing_photos.py --dry-run     # look up, attach nothing
    python scripts/fetch_missing_photos.py --only 12 --only 40

The password is read from ``SHELFOS_PASSWORD`` or asked for. The account must be
allowed to write (attaching a file is a writer's action), and every lookup spends
the shop's API quota, so lookups are paced: ``--delay`` apart for each shop, and
never faster than Mouser's 30 calls a minute (faster, and it answers "could not
read the Mouser response" for whole runs of parts).
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

# The order links are tried in; datasheet links never lead to a product page.
LINK_KINDS = ("shop", "product", "other")

# Host label → the shop key ``/api/shops/lookup`` takes for an index lookup.
# Matched as a domain label so country domains (mouser.pl, digikey.co.uk) count.
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
    host = (urlsplit(url).hostname or "").lower()
    for label in host.split("."):
        if label in SHOP_HOSTS:
            return SHOP_HOSTS[label]
    return None


def same_mpn(a: str | None, b: str | None) -> bool:
    """Whether two MPNs name the same part, ignoring case and punctuation."""

    def norm(value: str | None) -> str:
        return re.sub(r"[^0-9a-z]", "", (value or "").casefold())

    return bool(norm(a)) and norm(a) == norm(b)


def error_text(resp: httpx.Response) -> str:
    """The server's own message for a failed call, else its status."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, list):  # a FastAPI validation error
        detail = "; ".join(str(d.get("msg", d)) for d in detail)
    return (
        f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"
    )


@dataclass
class Report:
    attached: list[str] = field(default_factory=list)
    would_attach: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)

    def skip(self, reason: str, label: str) -> None:
        self.skipped.setdefault(reason, []).append(label)


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

    def lookup(
        self, shop: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """A shop lookup: (product, None) or (None, the server's reason)."""
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

    def attach_photo(self, component_id: int, url: str) -> str | None:
        """Have the server download ``url`` as a photo; None on success."""
        resp = self.client.post(
            "/api/attachments/from-url",
            json={
                "entity_type": "component",
                "entity_id": component_id,
                "url": url,
                "kind": "photo",
            },
        )
        return None if resp.status_code == 201 else error_text(resp)


def find_product(
    api: Api, row: dict[str, Any], links: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    """The shop's record for this component, verified by MPN, or why there is none."""
    mpn = row.get("mpn") or ""
    tried_shops: set[str] = set()
    last_reason = "no shop link"
    for link in links:
        url = link["url"]
        key = shop_key(url)
        if key is None:
            continue
        # The link as it stands: a real product page answers for this part.
        product, reason = api.lookup(key, {"code": url})
        if product and same_mpn(product.get("mpn"), mpn):
            return product, ""
        if product:
            reason = f"link led to {product.get('mpn') or 'an unnamed part'}"
        last_reason = reason or last_reason
        # A search link (or a moved page): ask the same shop by the part number.
        if key not in tried_shops:
            tried_shops.add(key)
            product, reason = api.lookup(
                key,
                {
                    "shop_key": key,
                    "part_numbers": [mpn],
                    "manufacturer": row.get("manufacturer") or None,
                },
            )
            if product and same_mpn(product.get("mpn"), mpn):
                return product, ""
            if product:
                reason = (
                    f"{key} answered with {product.get('mpn') or 'an unnamed part'}"
                )
            last_reason = reason or last_reason
    return None, last_reason


def run(api: Api, rows: list[dict[str, Any]], *, dry_run: bool) -> Report:
    report = Report()
    for n, row in enumerate(rows, 1):
        cid = row["id"]
        label = f"#{cid} {row.get('mpn') or '(no MPN)'}"
        print(f"[{n}/{len(rows)}] {label} … ", end="", flush=True)

        links = sorted(
            (link for link in api.links(cid) if link.get("kind") in LINK_KINDS),
            key=lambda link: LINK_KINDS.index(link["kind"]),
        )
        if not any(shop_key(link["url"]) for link in links):
            print("no shop link")
            report.skip("no shop link", label)
            continue
        if not row.get("mpn"):
            print("no MPN to verify the shop's answer against")
            report.skip("no MPN", label)
            continue

        product, reason = find_product(api, row, links)
        if product is None:
            print(f"not found ({reason})")
            report.skip("shop lookup failed", f"{label} — {reason}")
            continue
        image = product.get("image_url")
        if not image:
            print("the shop has no image for it")
            report.skip("shop has no image", label)
            continue
        if dry_run:
            print(f"would attach {image}")
            report.would_attach.append(label)
            continue
        error = api.attach_photo(cid, image)
        if error:
            print(f"download refused ({error})")
            # One shop refuses a whole batch with the same long message, so it
            # heads the group rather than repeating on every line.
            host = urlsplit(image).hostname or image
            report.skip(f"image download failed — {error}", f"{label} ({host})")
            continue
        print("photo attached")
        report.attached.append(label)
    return report


def print_summary(report: Report, *, dry_run: bool) -> None:
    print()
    if dry_run:
        print(f"Would attach a photo to {len(report.would_attach)} component(s).")
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
        rows = [row for row in rows if row["id"] in set(args.only)]
    print(f"{len(rows)} component(s) without a photo.\n")
    report = run(api, rows, dry_run=args.dry_run)
    print_summary(report, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
