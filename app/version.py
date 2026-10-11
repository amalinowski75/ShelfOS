"""Which build of ShelfOS this is, for the About dialog.

There are no releases yet, so the version is a pull request: every change
reaches main as a squash merge that GitHub commits under its own name, titled
"... (#185)", and the newest of those in this checkout's history says what the
build contains. Only GitHub's own commits count: a commit on a branch may end in
"(#185)" too, written by hand to point at its pull request, and that pull
request is not merged yet. What sits on top of the merge is said too, so a
development build does not pass for the merged one: commits of its own, and
edits not committed at all.

Asked of git, once per process and off the request path: the app warms it at
startup (see ``app.main``), and every deploy restarts the service. A failure is
not kept, so a git that was busy at startup is asked again a minute later.

A deployed ShelfOS is a git clone (see shelfos.sh) owned by root while the
service runs as its own user, which git refuses as "dubious ownership";
``safe.directory`` given on the command line is accepted, and touches nobody's
configuration.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Where links point when the clone's own origin is not a GitHub repository.
UPSTREAM_URL: Final = "https://github.com/amalinowski75/ShelfOS"

_ROOT: Final = Path(__file__).resolve().parent.parent
_PR_IN_SUBJECT: Final = re.compile(r"\(#(\d+)\)$")
# git@github.com:owner/repo.git, https://github.com/owner/repo(.git),
# ssh://git@github.com/owner/repo.git — the forms `git remote get-url` gives.
_GITHUB_REMOTE: Final = re.compile(
    r"^(?:https://|ssh://git@|git@)github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)
_GIT_SECONDS: Final = 5.0
# How long a failed read stands before git is asked again.
_RETRY_SECONDS: Final = 60.0


@dataclass(frozen=True)
class Version:
    """The newest merged pull request in this build, and what sits on top of it."""

    pr: int | None
    # Commits after that merge, merges of main into a branch not counted.
    ahead: int
    # HEAD, abbreviated; "" when git could not be asked.
    commit: str
    # Tracked files edited and not committed.
    dirty: bool
    # The repository the build came from, for the Source and pull-request links.
    repo_url: str = UPSTREAM_URL

    @property
    def pr_url(self) -> str:
        return f"{self.repo_url}/pull/{self.pr}" if self.pr is not None else ""

    @property
    def changelog_url(self) -> str:
        return f"{self.repo_url}/blob/main/CHANGELOG.md"


UNKNOWN: Final = Version(pr=None, ahead=0, commit="", dirty=False)


class _GitFailed(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitFailed(str(exc)) from exc
    return result.stdout.strip()


def repo_url(remote: str) -> str:
    """The web address of a GitHub remote, or the upstream's for anything else."""
    found = _GITHUB_REMOTE.match(remote.strip())
    return f"https://github.com/{found.group(1)}" if found else UPSTREAM_URL


def read(root: Path = _ROOT) -> Version:
    """Ask git about the checkout at ``root``; :data:`UNKNOWN` if it cannot say."""
    try:
        head = _git(root, "rev-parse", "--short", "HEAD")
        merge = _git(
            root,
            "log",
            "-1",
            "--committer=^GitHub ",
            "-E",
            r"--grep=\(#[0-9]+\)$",
            "--format=%h%x09%s",
        )
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    except _GitFailed:
        return UNKNOWN
    try:
        url = repo_url(_git(root, "remote", "get-url", "origin"))
    except _GitFailed:  # no origin: a clone of a clone, or a fresh init
        url = UPSTREAM_URL

    sha, _, subject = merge.partition("\t")
    found = _PR_IN_SUBJECT.search(subject.strip())
    if not found:
        # --grep matches any line of the message; a squash merge's body lists
        # the branch's commits, so the subject is checked again.
        return Version(pr=None, ahead=0, commit=head, dirty=dirty, repo_url=url)
    try:
        ahead = int(_git(root, "rev-list", "--count", "--no-merges", f"{sha}..HEAD"))
    except (_GitFailed, ValueError):
        return UNKNOWN
    return Version(
        pr=int(found.group(1)), ahead=ahead, commit=head, dirty=dirty, repo_url=url
    )


_lock = threading.Lock()
_known: Version | None = None
_failed_at: float | None = None


def current() -> Version:
    """This build's version: read once, kept once it has been read successfully.

    The lock makes concurrent first callers wait for one git run rather than
    each start their own.
    """
    global _known, _failed_at
    with _lock:
        if _known is not None:
            return _known
        if _failed_at is not None and time.monotonic() - _failed_at < _RETRY_SECONDS:
            return UNKNOWN
        found = read()
        if found == UNKNOWN:
            _failed_at = time.monotonic()
        else:
            _known, _failed_at = found, None
        return found


def forget() -> None:
    """Drop what :func:`current` has kept — for tests."""
    global _known, _failed_at
    with _lock:
        _known, _failed_at = None, None


def warm() -> None:
    """Read the version on a thread of its own, so no request waits for git."""
    threading.Thread(target=current, name="shelfos-version", daemon=True).start()
