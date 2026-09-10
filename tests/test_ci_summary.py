"""Tests for the CI job-summary renderer.

The summary is the thing a reader looks at when CI goes red, so the case that
matters most is the one where tests failed: the count has to be right and the
failing test has to be named. These use JUnit XML shaped the way pytest and
vitest actually write it, including the two places they disagree (a
``<testsuites>`` wrapper, and where the wall time lives).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.ci_summary import _one_line, main, parse, render

# pytest writes <testsuites> wrapping a single <testsuite>, dotted module paths
# in classname, and reports a fixture explosion as <error> rather than
# <failure>. All three are represented here.
PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="12.5">
    <testcase classname="tests.test_stock" name="test_take" time="0.10"/>
    <testcase classname="tests.test_stock" name="test_over" time="0.20">
      <failure message="AssertionError: 3 != 4">long traceback</failure>
    </testcase>
    <testcase classname="tests.test_stock" name="test_slow" time="0.30">
      <skipped message="no printer here"/>
    </testcase>
    <testcase classname="tests.test_bom" name="test_setup" time="0.40">
      <error message="RuntimeError: fixture blew up">trace</error>
    </testcase>
  </testsuite>
</testsuites>
"""

# vitest puts the wall time on the root, the file path in classname, and runs
# files in parallel — so the per-case times sum to more than the elapsed time.
VITEST_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="0" errors="0" time="4.78">
  <testsuite name="tests/js/app.test.js" tests="2" failures="0" time="9.0">
    <testcase classname="tests/js/app.test.js" name="formats a row" time="4.5"/>
    <testcase classname="tests/js/app.test.js" name="escapes a cell" time="4.5"/>
  </testsuite>
</testsuites>
"""


def write(tmp_path: Path, body: str) -> Path:
    report = tmp_path / "report.xml"
    report.write_text(body, encoding="utf-8")
    return report


def test_counts_every_outcome_separately(tmp_path):
    totals = parse(write(tmp_path, PYTEST_XML))

    assert (totals.tests, totals.passed) == (4, 1)
    assert (totals.failures, totals.errors, totals.skipped) == (1, 1, 1)


def test_a_failure_and_an_error_are_both_named(tmp_path):
    # A fixture that raises is an <error>, not a <failure>. Reporting only
    # <failure> would leave the run red with an empty "what did not pass".
    totals = parse(write(tmp_path, PYTEST_XML))

    assert [c.label for c in totals.bad] == [
        "tests.test_stock › test_over",
        "tests.test_bom › test_setup",
    ]


def test_wall_time_comes_from_the_root_not_the_sum(tmp_path):
    # The cases add up to 9 seconds across two parallel files that took 4.78.
    totals = parse(write(tmp_path, VITEST_XML))

    assert totals.time == pytest.approx(4.78)


def test_a_green_run_says_so_and_lists_nothing(tmp_path):
    block = render("Web (vitest)", parse(write(tmp_path, VITEST_XML)))

    assert block.startswith("### ✅ Web (vitest)")
    assert "What did not pass" not in block


def test_a_red_run_leads_with_the_cross_and_the_failure(tmp_path):
    block = render("Python (pytest)", parse(write(tmp_path, PYTEST_XML)))

    assert block.startswith("### ❌ Python (pytest)")
    assert "| 4 | 1 | 1 | 1 | 1 | 12.5s |" in block
    assert "`tests.test_stock › test_over`" in block
    assert "AssertionError: 3 != 4" in block


def test_a_pipe_in_a_message_cannot_break_the_table():
    # An assertion that compares strings containing "|" is ordinary, and an
    # unescaped one silently splits the row into extra columns.
    assert _one_line("assert 'a|b' == 'a|c'") == "assert 'a\\|b' == 'a\\|c'"


def test_a_multi_line_message_is_flattened():
    assert _one_line("first\n  second\n\tthird") == "first second third"


def test_a_long_message_is_truncated_to_one_cell():
    line = _one_line("x" * 500)

    assert len(line) == 160
    assert line.endswith("…")


def test_a_missing_report_is_reported_rather_than_crashing(
    tmp_path, capsys, monkeypatch
):
    # The suite died before writing XML (an import error, a killed worker).
    # The summary step must still say something, and must not fail the job a
    # second time with its own traceback.
    #
    # The unset is not optional: these tests themselves run inside Actions,
    # where GITHUB_STEP_SUMMARY is set, and the output would go to that file
    # instead of stdout. Read from the ambient environment, this passed
    # locally and failed in CI.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    code = main(["--title", "Python (pytest)", str(tmp_path / "absent.xml")])

    assert code == 0
    assert "No test report was written." in capsys.readouterr().out


def test_the_summary_is_appended_so_two_suites_can_share_a_file(tmp_path, monkeypatch):
    # Both jobs write to $GITHUB_STEP_SUMMARY. Opening it for writing rather
    # than appending would leave whichever finished last as the only one shown.
    destination = tmp_path / "summary.md"
    destination.write_text("### earlier\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(destination))

    main(["--title", "Web (vitest)", str(write(tmp_path, VITEST_XML))])

    written = destination.read_text(encoding="utf-8")
    assert written.startswith("### earlier\n")
    assert "### ✅ Web (vitest)" in written


def test_it_runs_as_a_script_the_way_the_workflow_invokes_it(tmp_path):
    # The workflow calls it with `python scripts/ci_summary.py`, not as an
    # imported module. A relative import or a missing __main__ guard would pass
    # every test above and still fail in CI.
    report = write(tmp_path, VITEST_XML)
    script = Path(__file__).resolve().parents[1] / "scripts" / "ci_summary.py"
    # Inherit the environment minus the one variable that would redirect the
    # output into a file: under Actions these tests run with it set.
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}

    done = subprocess.run(
        [sys.executable, str(script), "--title", "Web (vitest)", str(report)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )

    assert "### ✅ Web (vitest)" in done.stdout
