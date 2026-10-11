"""Which build this is, asked of git (app/version.py).

Each test builds a small repository of its own, with commits made under
GitHub's name where a squash merge would be, so what is checked is git's answer
to the questions the module asks, not a parser fed a hand-written log.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from app import version

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="no git here")


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q", "-b", "main")
        self._n = 0

    def git(self, *args: str, committer: str = "Someone") -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Someone",
            "GIT_AUTHOR_EMAIL": "someone@example.com",
            "GIT_COMMITTER_NAME": committer,
            "GIT_COMMITTER_EMAIL": "noreply@github.com"
            if committer == "GitHub"
            else "someone@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()

    def commit(
        self, subject: str, *, committer: str = "Someone", body: str = ""
    ) -> str:
        self._n += 1
        (self.root / "file.txt").write_text(f"{self._n}\n")
        self.git("add", "file.txt")
        message = ["-m", subject] + (["-m", body] if body else [])
        self.git("commit", "-q", *message, committer=committer)
        return self.git("rev-parse", "--short", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_a_build_of_main_is_its_newest_merged_pull_request(repo: Repo) -> None:
    repo.commit("Labels (#184)", committer="GitHub")
    head = repo.commit("The logo on labels (#185)", committer="GitHub")
    assert version.read(repo.root) == version.Version(
        pr=185, ahead=0, commit=head, dirty=False
    )


def test_a_branch_commit_naming_its_own_pull_request_is_not_a_merge(
    repo: Repo,
) -> None:
    repo.commit("The logo on labels (#185)", committer="GitHub")
    repo.commit("Add the About dialog")
    head = repo.commit("Changelog: number the entry (#186)")
    found = version.read(repo.root)
    assert (found.pr, found.ahead, found.commit) == (185, 2, head)


def test_a_branch_that_merged_main_in_counts_its_own_commits(repo: Repo) -> None:
    """Cut from #180, three commits, then main (#181, #182) merged in: the
    branch is #182 plus its three commits, whatever order the dates give."""
    repo.commit("Old work (#180)", committer="GitHub")
    repo.git("checkout", "-q", "-b", "work")
    for n in range(3):
        repo.commit(f"Branch step {n}")
    repo.git("checkout", "-q", "main")
    (repo.root / "other.txt").write_text("main\n")
    repo.git("add", "other.txt")
    repo.git("commit", "-q", "-m", "Something (#181)", committer="GitHub")
    repo.git("commit", "-q", "--allow-empty", "-m", "Else (#182)", committer="GitHub")
    repo.git("checkout", "-q", "work")
    repo.git("merge", "-q", "--no-edit", "main")
    found = version.read(repo.root)
    assert (found.pr, found.ahead) == (182, 3)


def test_only_the_subject_names_the_pull_request(repo: Repo) -> None:
    """A squash merge's body lists the branch's commits, some ending in (#N)."""
    head = repo.commit(
        "Fix a thing", committer="GitHub", body="* Changelog: number it (#99)"
    )
    found = version.read(repo.root)
    assert (found.pr, found.commit) == (None, head)


def test_uncommitted_edits_are_reported_and_untracked_files_are_not(
    repo: Repo,
) -> None:
    repo.commit("Labels (#185)", committer="GitHub")
    (repo.root / "scratch.txt").write_text("not ours\n")
    assert version.read(repo.root).dirty is False
    (repo.root / "file.txt").write_text("edited\n")
    assert version.read(repo.root).dirty is True


def test_links_follow_the_clone_s_own_github_origin(repo: Repo) -> None:
    repo.commit("Labels (#12)", committer="GitHub")
    repo.git("remote", "add", "origin", "git@github.com:someone/ShelfOS-fork.git")
    found = version.read(repo.root)
    assert found.pr_url == "https://github.com/someone/ShelfOS-fork/pull/12"
    assert found.changelog_url == (
        "https://github.com/someone/ShelfOS-fork/blob/main/CHANGELOG.md"
    )


@pytest.mark.parametrize(
    ("remote", "url"),
    [
        ("git@github.com:a/ShelfOS.git", "https://github.com/a/ShelfOS"),
        ("https://github.com/a/ShelfOS", "https://github.com/a/ShelfOS"),
        ("https://github.com/a/b.git", "https://github.com/a/b"),
        ("ssh://git@github.com/a/b.git", "https://github.com/a/b"),
        # Not GitHub, or not a repository: the upstream it came from.
        ("/home/someone/vcs/ShelfOS", version.UPSTREAM_URL),
        ("https://gitlab.com/a/b.git", version.UPSTREAM_URL),
        ("https://github.com.evil.example/a/b", version.UPSTREAM_URL),
    ],
)
def test_repo_url_from_a_remote(remote: str, url: str) -> None:
    assert version.repo_url(remote) == url


def test_a_history_with_no_merge_in_it_is_a_development_build(repo: Repo) -> None:
    head = repo.commit("Initial import")
    found = version.read(repo.root)
    assert (found.pr, found.commit, found.pr_url) == (None, head, "")


def test_somewhere_that_is_not_a_checkout_is_unknown(tmp_path: Path) -> None:
    assert version.read(tmp_path) == version.UNKNOWN


def test_a_failure_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git busy once at startup must not leave "unknown" until the next restart."""
    merged = version.Version(pr=185, ahead=0, commit="c9b38f6", dirty=False)
    answers = [version.UNKNOWN, merged]
    monkeypatch.setattr(version, "read", lambda: answers.pop(0))
    clock = [1000.0]
    monkeypatch.setattr(version.time, "monotonic", lambda: clock[0])
    version.forget()
    try:
        assert version.current() == version.UNKNOWN
        clock[0] += 10  # within the minute: not asked again
        assert version.current() == version.UNKNOWN
        assert len(answers) == 1
        clock[0] += 60
        assert version.current().pr == 185
        assert version.current().pr == 185  # and that one is kept
    finally:
        version.forget()


def test_the_app_asks_git_at_startup_not_on_the_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app
    from fastapi.testclient import TestClient

    warmed: list[bool] = []
    monkeypatch.setattr(version, "warm", lambda: warmed.append(True))
    with TestClient(create_app(create_tables=False)):
        assert warmed == [True]
