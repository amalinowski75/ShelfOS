#!/usr/bin/env python3
"""Render a JUnit XML report into a GitHub Actions job summary.

CI runs two suites written in two languages with two runners, and until now the
only way to learn how many tests ran was to open the log and read to the end.
This turns either runner's JUnit XML into the table GitHub shows at the top of
the run, so the count, the timing and the name of anything that failed are
visible without expanding a single step.

Deliberately stdlib-only and deliberately not a third-party action: a reporter
action needs write permission on checks or pull requests, and pinning one
safely means chasing its commit SHA on every bump. Reading a file we just
produced needs neither.

Usage::

    python scripts/ci_summary.py --title "Python (pytest)" junit/pytest.xml

Writes to ``$GITHUB_STEP_SUMMARY`` when that is set (so it lands in the run's
summary page) and to stdout otherwise, which is what happens when a developer
runs it locally. Exit status is always 0: this reports on the suite, it does not
judge it. The suite's own step already failed the job if anything went wrong,
and a summary step that can fail would mask that with a second, less useful
error.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

# How many failures to name in full. Past this the table is scrolling rather
# than summarising, and the log is the better tool.
MAX_LISTED = 20


@dataclass
class Case:
    """One ``<testcase>``: where it lives, how it went, how long it took."""

    classname: str
    name: str
    time: float
    # Set by the caller once the child elements say how it went; "passed" is
    # the state a <testcase> with no <failure>, <error> or <skipped> is in.
    status: str = "passed"
    message: str = ""

    @property
    def label(self) -> str:
        # pytest fills classname with the dotted module path; vitest puts the
        # file there. Either way it is the part that says *where*, and joining
        # with the test name reads the way the runner prints it.
        return f"{self.classname} › {self.name}" if self.classname else self.name


@dataclass
class Totals:
    """The counts a reader actually wants, plus the cases worth naming."""

    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    time: float = 0.0
    bad: list[Case] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped


def _float(value: str | None) -> float:
    """Parse a duration attribute, treating anything unparseable as zero.

    Durations are decoration. A runner that writes ``time="N/A"`` should cost us
    a blank column, not a crashed summary step on an otherwise green run.
    """
    try:
        return float(value or 0.0)
    except ValueError:
        return 0.0


def parse(path: Path) -> Totals:
    """Read one JUnit XML file into totals.

    Counts are recomputed from the ``<testcase>`` elements rather than read off
    the ``<testsuite>`` attributes. Both runners write those attributes, but
    they disagree about whether a suite's numbers include its children, and a
    recount is cheap and cannot disagree with the list of failures below it.
    """
    totals = Totals()
    root = ElementTree.parse(path).getroot()

    # The root is <testsuites> for vitest and (usually) <testsuite> for pytest.
    # iter() over testcase covers both without caring which.
    for case in root.iter("testcase"):
        entry = Case(
            classname=case.get("classname", ""),
            name=case.get("name", ""),
            time=_float(case.get("time")),
        )
        totals.tests += 1

        if (failure := case.find("failure")) is not None:
            entry.status = "failed"
            entry.message = failure.get("message", "") or (failure.text or "")
            totals.failures += 1
            totals.bad.append(entry)
        elif (error := case.find("error")) is not None:
            entry.status = "error"
            entry.message = error.get("message", "") or (error.text or "")
            totals.errors += 1
            totals.bad.append(entry)
        elif case.find("skipped") is not None:
            entry.status = "skipped"
            totals.skipped += 1
        else:
            entry.status = "passed"

    # Wall time comes off the root element, not from summing the cases: vitest
    # runs files in parallel, so the sum of the parts is several times the time
    # anyone actually waited. Fall back to the sum only if the root is silent.
    totals.time = _float(root.get("time")) or sum(
        _float(s.get("time")) for s in root.iter("testsuite")
    )
    return totals


def _one_line(text: str, limit: int = 160) -> str:
    """Squeeze an assertion message onto one table row.

    Pipes and newlines would both break out of a Markdown table cell, so they go
    first; the truncation is what keeps one enormous diff from pushing the rest
    of the table off the page.
    """
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def render(title: str, totals: Totals) -> str:
    """Build the Markdown block for one suite."""
    verdict = "❌" if (totals.failures or totals.errors) else "✅"
    lines = [
        f"### {verdict} {title}",
        "",
        "| Tests | Passed | Failed | Errors | Skipped | Time |",
        "| ----: | -----: | -----: | -----: | ------: | ---: |",
        f"| {totals.tests} | {totals.passed} | {totals.failures} "
        f"| {totals.errors} | {totals.skipped} | {totals.time:.1f}s |",
        "",
    ]

    if totals.bad:
        shown = totals.bad[:MAX_LISTED]
        lines += [
            "<details open><summary>What did not pass</summary>",
            "",
            "| Test | Why |",
            "| ---- | --- |",
        ]
        lines += [f"| `{c.label}` | {_one_line(c.message)} |" for c in shown]
        if len(totals.bad) > len(shown):
            lines.append(
                f"| … | and {len(totals.bad) - len(shown)} more; see the log |"
            )
        lines += ["", "</details>", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JUnit XML written by the runner")
    parser.add_argument("--title", required=True, help="Heading for this suite")
    args = parser.parse_args(argv)

    if not args.report.is_file():
        # A missing report means the suite died before it could write one — an
        # import error, a crashed worker. Say so in the summary instead of
        # leaving the reader with a heading and no table, and still exit 0: the
        # suite's own step has already failed the job.
        block = f"### ⚠️ {args.title}\n\nNo test report was written.\n"
    else:
        block = render(args.title, parse(args.report))

    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(block + "\n")
    else:
        sys.stdout.write(block + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
