"""Which build of ShelfOS this is, for the About dialog.

There are no releases yet, so the version is a pull request: every change
reaches main as a squash merge that GitHub commits under its own name, titled
"... (#185)", and the newest of those in this checkout's history says what the
build contains. Commits on top of it (a branch being worked on) are counted, so
a development build does not pass for the merged one. Only GitHub's own commits
count: a commit on a branch may end in "(#185)" too, written by hand to point at
its pull request, and that pull request is not merged yet.

Read from git once, at first use. A deployed ShelfOS is a git clone (see
shelfos.sh) and every update restarts its service. The clone belongs to root
while the service runs as its own user, which git refuses as "dubious
ownership"; ``safe.directory`` given on the command line is accepted, and
touches nobody's configuration.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

REPO_URL: Final = "https://github.com/amalinowski75/ShelfOS"
CHANGELOG_URL: Final = f"{REPO_URL}/blob/main/CHANGELOG.md"

_ROOT: Final = Path(__file__).resolve().parent.parent
_MERGED_PR: Final = re.compile(r"\(#(\d+)\)$")
# How far back to look for a merge: far past any branch anyone keeps going.
_SEARCH_DEPTH: Final = 500
_GIT_SECONDS: Final = 5.0


@dataclass(frozen=True)
class Version:
    """The newest merged pull request in this build, and what sits on top of it."""

    pr: int | None
    # Commits after that merge; 0 for a build of main itself.
    ahead: int
    # HEAD, abbreviated; "" when git could not be asked.
    commit: str

    @property
    def pr_url(self) -> str:
        return f"{REPO_URL}/pull/{self.pr}" if self.pr is not None else ""


UNKNOWN: Final = Version(pr=None, ahead=0, commit="")


def parse_log(log: str) -> Version:
    """The version from ``git log --format='%h%x09%cn%x09%s'`` output, newest first."""
    rows = [line.split("\t", 2) for line in log.splitlines() if line.count("\t") >= 2]
    if not rows:
        return UNKNOWN
    head = rows[0][0]
    for ahead, (_, committer, subject) in enumerate(rows):
        found = _MERGED_PR.search(subject.strip())
        if committer == "GitHub" and found:
            return Version(pr=int(found.group(1)), ahead=ahead, commit=head)
    return Version(pr=None, ahead=len(rows), commit=head)


@lru_cache(maxsize=1)
def current() -> Version:
    """This build's version, or :data:`UNKNOWN` where there is no git to ask."""
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-C",
                str(_ROOT),
                "log",
                f"-{_SEARCH_DEPTH}",
                "--format=%h%x09%cn%x09%s",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return parse_log(result.stdout)
