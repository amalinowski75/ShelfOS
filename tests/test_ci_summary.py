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
from scripts.ci_summary import _code, _one_line, main, parse, render

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
    assert "No readable test report was written." in capsys.readouterr().out


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


def test_an_empty_report_is_reported_rather_than_crashing(
    tmp_path, capsys, monkeypatch
):
    # Vitest opens its output file when the run starts and writes it when the
    # run ends, so a killed worker (or this workflow's own cancel-in-progress)
    # leaves the file there and empty. That is not a missing file, and
    # ElementTree raises on it.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    empty = tmp_path / "vitest.xml"
    empty.write_text("", encoding="utf-8")

    code = main(["--title", "Web (vitest)", str(empty)])

    assert code == 0
    assert "No readable test report was written." in capsys.readouterr().out


def test_a_truncated_report_is_reported_rather_than_crashing(
    tmp_path, capsys, monkeypatch
):
    # The other half of the same failure: the runner died mid-write.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    cut = tmp_path / "vitest.xml"
    cut.write_text(VITEST_XML[: len(VITEST_XML) // 2], encoding="utf-8")

    code = main(["--title", "Web (vitest)", str(cut)])

    assert code == 0
    assert "No readable test report was written." in capsys.readouterr().out


def test_a_pipe_in_a_test_name_cannot_break_the_table(tmp_path):
    # A parametrized id is ordinary and can hold anything, including the one
    # character that ends a Markdown cell.
    body = PYTEST_XML.replace('name="test_over"', 'name="test_rate[10k|1%]"')
    block = render("Python (pytest)", parse(write(tmp_path, body)))

    row = next(line for line in block.splitlines() if "test_rate" in line)
    assert "10k\\|1%" in row  # escaped, so it stays inside its cell
    # Two edges and one divider. Escaped pipes do not count: they are content.
    assert row.count("|") - row.count("\\|") == 3


def test_a_backtick_in_a_test_name_cannot_end_its_code_span():
    # The name is rendered inside a code span; a backtick in it would close
    # that span early and spill the rest into the table as markup.
    assert _code("a `b` c") == "``a `b` c``"
    # The name both contains and ends with a backtick, so the fence grows to
    # two and the content is padded away from it.
    assert _code("it renders `code`") == "`` it renders `code` ``"


def test_a_name_that_starts_or_ends_with_a_backtick_is_padded():
    # Markdown strips one leading and trailing space inside a span, so the
    # padding keeps the backtick as content rather than as fence.
    assert _code("`quoted`") == "`` `quoted` ``"


# A suite with one test worth noticing and two that are not, so the floor has
# something to cut.
SLOW_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3" time="9.0">
    <testcase classname="tests.test_deploy" name="test_walks_the_steps" time="5.5"/>
    <testcase classname="tests.test_api" name="test_changes_a_password" time="2.4"/>
    <testcase classname="tests.test_units" name="test_parses_a_value" time="0.01"/>
  </testsuite>
</testsuites>
"""


def test_the_slowest_tests_are_named_worst_first(tmp_path):
    totals = parse(write(tmp_path, SLOW_XML))

    assert [c.name for c in totals.slowest[:2]] == [
        "test_walks_the_steps",
        "test_changes_a_password",
    ]


def test_the_slowest_block_leaves_out_the_quick_ones(tmp_path):
    # Naming a 10ms test among the slowest is noise, and noise is what trains
    # a reader to skip the block on the day it matters.
    block = render("Python (pytest)", parse(write(tmp_path, SLOW_XML)))

    assert "Slowest tests" in block
    assert "test_walks_the_steps" in block
    assert "test_parses_a_value" not in block


def test_a_suite_with_nothing_slow_gets_no_block(tmp_path):
    block = render(
        "Web (vitest)", parse(write(tmp_path, VITEST_XML.replace('"4.5"', '"0.4"')))
    )

    assert "Slowest tests" not in block


def test_the_slowest_block_is_closed_and_the_failures_block_is_open(tmp_path):
    # Failures are why you opened the page; timings are what you go looking
    # for. Only one of them should be expanded.
    body = PYTEST_XML.replace(
        'name="test_take" time="0.10"', 'name="test_take" time="7.0"'
    )
    block = render("Python (pytest)", parse(write(tmp_path, body)))

    assert "<details open><summary>What did not pass</summary>" in block
    assert "<details><summary>Slowest tests</summary>" in block


def test_a_measured_elapsed_time_overrides_the_reports_own(
    tmp_path, capsys, monkeypatch
):
    # Under -n auto pytest writes a duration that is neither the wall clock
    # nor the sum of the tests, so the workflow measures it and passes it in.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    main(
        [
            "--title",
            "Python (pytest)",
            "--elapsed",
            "90",
            str(write(tmp_path, SLOW_XML)),
        ]
    )

    out = capsys.readouterr().out
    assert "| 90.0s |" in out
    assert "9.0s" not in out


def test_a_zero_elapsed_is_read_as_no_measurement(tmp_path, capsys, monkeypatch):
    # The workflow sources this from a shell variable that is unset whenever
    # the measuring step did not finish. A default of 0 reaching here would
    # render a table claiming the suite was instant, which is a worse lie than
    # the report's own bad figure.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    main(
        ["--title", "Python (pytest)", "--elapsed", "0", str(write(tmp_path, SLOW_XML))]
    )

    out = capsys.readouterr().out
    assert "| 9.0s |" in out  # the report's figure, not the zero
    assert "0.0s |" not in out
