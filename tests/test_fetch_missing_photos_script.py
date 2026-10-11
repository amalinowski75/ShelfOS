"""How scripts/fetch_missing_photos.py reads a shop lookup that failed.

The script cannot see a status of its own — the API passes every shop failure
through as a 422 — so it reads the providers' messages. Which of them are worth a
retry, and which mean the shop could not answer at all, decides how long a run
takes and whether it exits 1.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts import fetch_missing_photos as script

_REFUSAL = "HTTP 422: Mouser rejected the request "
DAILY = _REFUSAL + "(HTTP 403): Maximum calls per day exceeded."
PER_MINUTE = _REFUSAL + "(HTTP 429): Maximum calls per minute exceeded."


class Recorder:
    """An Api whose every lookup answers ``reason``, counting the calls made."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *reasons: str) -> None:
        monkeypatch.setattr(script, "RETRY_AFTER", 0)
        self.api = script.Api("http://shelfos.invalid", 0)
        self.calls: list[str] = []
        answers = iter(reasons)

        def once(shop: str, body: dict[str, Any]) -> tuple[None, str]:
            self.calls.append(shop)
            return None, next(answers, reasons[-1])

        monkeypatch.setattr(self.api, "_lookup_once", once)


@pytest.mark.parametrize(
    ("reason", "calls"),
    [
        (DAILY, 1),  # nothing works until tomorrow
        (PER_MINUTE, 2),
        ("HTTP 422: Mouser rejected the request (HTTP 503): Service unavailable", 2),
        ("HTTP 422: could not read the Mouser response", 2),
        ("HTTP 422: could not reach Mouser", 2),  # a dropped connection, a timeout
        ("HTTP 422: COULD NOT READ THE MOUSER RESPONSE", 2),  # wording may vary
        ("HTTP 422: no product found", 1),  # a miss is an answer
        ("HTTP 422: Mouser rejected the request: Invalid unique identifier.", 1),
    ],
)
def test_which_failures_are_retried(
    monkeypatch: pytest.MonkeyPatch, reason: str, calls: int
) -> None:
    rec = Recorder(monkeypatch, reason)

    rec.api.lookup("mouser", {})

    assert len(rec.calls) == calls


@pytest.mark.parametrize(
    ("reason", "down"),
    [
        (DAILY, True),
        (PER_MINUTE, True),
        ("HTTP 422: Mouser rejected the request (HTTP 401): Invalid key", True),
        ("HTTP 422: could not reach Mouser", True),
        ("HTTP 422: could not read the Digi-Key token response", True),
        ("HTTP 422: Digi-Key integration is not configured", True),
        ("HTTP 502", True),
        ("farnell: not asked, out of time", True),
        ("HTTP 422: no product found", False),
        ("HTTP 422: Mouser rejected the request: Invalid unique identifier.", False),
        ("HTTP 422: Digi-Key rejected the request: Duplicate Products found", False),
    ],
)
def test_which_failures_mean_the_shop_could_not_answer(reason: str, down: bool) -> None:
    assert script.unavailable(reason) is down


def test_a_spent_daily_quota_is_not_asked_again_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each further call would only repeat it, paced at 2.1 s for Mouser."""
    rec = Recorder(monkeypatch, DAILY)

    first = rec.api.lookup("mouser", {})
    later = [rec.api.lookup("mouser", {}) for _ in range(3)]
    other = rec.api.lookup("digikey", {})

    assert rec.calls == ["mouser", "digikey"]
    assert all(answer == first for answer in later)  # said, not swallowed
    assert other == (None, DAILY)  # another shop is still asked
