#!/usr/bin/env python3
"""Unit tests for charter_bloat_check.analyze -- pure function, no gh/network calls.

fk#753: this replaces "reconstruct each member's consolidation history from memory" with a
mechanical check. These tests pin the two behaviors that check depends on: a net-reductive PR
resets the counter, and `gh pr list --search "X in:files"`-style false positives (a PR that
never actually touched the file) must not count at all.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import charter_bloat_check as cbc  # noqa: E402


def _pr(number, merged_at, path, additions, deletions):
    return {
        "number": number,
        "mergedAt": merged_at,
        "files": [{"path": path, "additions": additions, "deletions": deletions}],
    }


def test_all_additive_since_start_counts_every_pr():
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 10, 0),
        _pr(2, "2026-09-02T00:00:00Z", "members/x/x.md", 8, 1),
        _pr(3, "2026-09-03T00:00:00Z", "members/x/x.md", 5, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 3
    assert r["last_consolidation_pr"] is None
    assert r["needs_consolidation"] is False  # below threshold of 5


def test_net_reductive_pr_resets_the_counter():
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 10, 0),
        _pr(2, "2026-09-02T00:00:00Z", "members/x/x.md", 20, 0),
        _pr(3, "2026-09-03T00:00:00Z", "members/x/x.md", 5, 40),  # consolidation
        _pr(4, "2026-09-04T00:00:00Z", "members/x/x.md", 3, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 1  # only PR#4, after the consolidation
    assert r["last_consolidation_pr"] == 3
    assert r["last_consolidation_date"] == "2026-09-03T00:00:00Z"


def test_needs_consolidation_flag_trips_at_five():
    prs = [_pr(n, f"2026-09-0{n}T00:00:00Z", "members/x/x.md", 4, 0) for n in range(1, 6)]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 5
    assert r["needs_consolidation"] is True


def test_pr_that_never_touched_the_file_is_not_counted():
    # The bug fk#753 flags in `gh pr list --search "X in:files"`: a PR whose body/title merely
    # MENTIONS members/x/x.md must not count just because search text-matched it.
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/y/y.md", 10, 0),  # different file entirely
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 0
    assert r["last_consolidation_pr"] is None
    assert r["needs_consolidation"] is False


def test_zero_diff_pr_never_counts_as_consolidation():
    # additions == deletions == 0 (e.g. a rename with no content change) must not
    # spuriously reset the counter via the `delete >= add` check.
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 0, 0),
        _pr(2, "2026-09-02T00:00:00Z", "members/x/x.md", 3, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 2
    assert r["last_consolidation_pr"] is None


def test_tied_mergedat_mixing_int_and_str_number_does_not_raise():
    # A squash-merged PR (`number` is int) and a direct push (`number` is `sha[:9]`, a str) can
    # land within the same calendar second. analyze() must not compare int < str while sorting.
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 10, 0),
        _pr("abc123def", "2026-09-01T00:00:00Z", "members/x/x.md", 5, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 2
    assert r["last_consolidation_pr"] is None


def _root_with_charter(td, rel, n_lines):
    path = os.path.join(td, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("line\n" * n_lines)
    return td


def test_charter_over_the_line_ceiling_flags_even_with_zero_churn():
    """A long charter is a per-pass cost whatever its churn ratio (marie.md: 646 lines and
    `ok` on every run, because one net-reductive PR resets `since_consolidation` forever)."""
    prs = [_pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 5, 40)]  # consolidation, since=0
    with tempfile.TemporaryDirectory() as td:
        _root_with_charter(td, "members/x/x.md", 600)
        r = cbc.analyze(["members/x/x.md"], prs, root=td, line_ceiling=450)["members/x/x.md"]
    assert r["count_since_consolidation"] == 0, r
    assert r["lines"] == 600
    assert r["over_ceiling"] is True
    assert r["needs_consolidation"] is True


def test_charter_under_the_line_ceiling_stays_ok():
    prs = [_pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 5, 40)]
    with tempfile.TemporaryDirectory() as td:
        _root_with_charter(td, "members/x/x.md", 100)
        r = cbc.analyze(["members/x/x.md"], prs, root=td, line_ceiling=450)["members/x/x.md"]
    assert r["lines"] == 100
    assert r["over_ceiling"] is False
    assert r["needs_consolidation"] is False


def test_unreadable_charter_never_flags_on_the_ceiling():
    """fk#908's rule, applied to the new arm: never print a verdict over data we did not read."""
    prs = [_pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 5, 40)]
    with tempfile.TemporaryDirectory() as td:  # file deliberately absent
        r = cbc.analyze(["members/x/x.md"], prs, root=td, line_ceiling=450)["members/x/x.md"]
    assert r["lines"] is None
    assert r["over_ceiling"] is False
    assert r["needs_consolidation"] is False


def test_churn_still_flags_independently_of_the_ceiling():
    prs = [_pr(i, f"2026-09-0{i}T00:00:00Z", "members/x/x.md", 10, 0) for i in range(1, 7)]
    with tempfile.TemporaryDirectory() as td:
        _root_with_charter(td, "members/x/x.md", 20)
        r = cbc.analyze(["members/x/x.md"], prs, root=td, line_ceiling=450)["members/x/x.md"]
    assert r["over_ceiling"] is False
    assert r["needs_consolidation"] is True  # churn arm alone


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
