"""Which build this is, read from the git history (app/version.py)."""

from __future__ import annotations

import subprocess

import pytest
from app import version


def _log(*rows: tuple[str, str, str]) -> str:
    return "".join(f"{sha}\t{who}\t{subject}\n" for sha, who, subject in rows)


def test_a_build_of_main_is_its_newest_merged_pull_request() -> None:
    log = _log(
        ("c9b38f6", "GitHub", "The ShelfOS logo on printed labels (#185)"),
        ("b002abf", "GitHub", "The ShelfOS icon and logo (#184)"),
    )
    assert version.parse_log(log) == version.Version(pr=185, ahead=0, commit="c9b38f6")


def test_a_branch_counts_its_commits_and_ignores_its_own_pr_number() -> None:
    """A commit on a branch may name its pull request; that one is not merged."""
    log = _log(
        ("97580c9", "Adam Malinowski", "Changelog: number the entry (#186)"),
        ("1a2b3c4", "Adam Malinowski", "Add the About dialog"),
        ("c9b38f6", "GitHub", "The ShelfOS logo on printed labels (#185)"),
    )
    assert version.parse_log(log) == version.Version(pr=185, ahead=2, commit="97580c9")
    assert version.parse_log(log).pr_url.endswith("/ShelfOS/pull/185")


def test_a_history_with_no_merge_in_it_is_a_development_build() -> None:
    log = _log(("1a2b3c4", "Someone", "Initial import"))
    found = version.parse_log(log)
    assert (found.pr, found.commit, found.pr_url) == (None, "1a2b3c4", "")


def test_no_git_output_is_unknown() -> None:
    assert version.parse_log("") == version.UNKNOWN


def test_no_git_at_all_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    version.current.cache_clear()
    monkeypatch.setattr(subprocess, "run", missing)
    try:
        assert version.current() == version.UNKNOWN
    finally:
        version.current.cache_clear()


def test_this_checkout_has_a_version() -> None:
    """Asked of the real git: whatever the history, HEAD is named."""
    version.current.cache_clear()
    try:
        found = version.current()
    finally:
        version.current.cache_clear()
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if not head:
        pytest.skip("not a git checkout")
    assert found.commit == head
