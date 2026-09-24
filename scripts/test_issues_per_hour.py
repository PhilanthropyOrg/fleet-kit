"""Tests for issues_per_hour.py -- the headline "issues resolved per hour" metric.

Reif's definition, pinned as three fixtures: a mega with 3 folded children, closed by a merged
AND deployed PR, is worth 3; a dupe/stale close (no merged-PR Closes-ref at all) is worth 0; a
merged-but-undeployed PR's closes are worth 0 until a deploy run ships its sha.

Run: python3 scripts/test_issues_per_hour.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issues_per_hour as iph  # noqa: E402


def _ancestor_factory(shipped_by_sha: set[str]):
    """Fake is_ancestor: sha `c` is deployed by run-sha `d` iff c == d and d is in the shipped
    set -- good enough to exercise deployed_by()'s "first run that shipped it" search without a
    real git repo."""
    return lambda c, d: c == d and d in shipped_by_sha


def _pr(number, merged_at, sha, closes):
    return {"number": number, "mergedAt": merged_at, "mergeCommit": {"oid": sha},
            "closingIssuesReferences": [{"number": n} for n in closes]}


def _deploy(sha, created_at, conclusion="success"):
    return {"headSha": sha, "createdAt": created_at, "conclusion": conclusion}


class MegaCreditTests(unittest.TestCase):
    """Fixture 1: a mega issue #500 folding 3 children (#501, #502, #503), closed by a PR that
    merged and later deployed. Worth 3 (the mega itself carries no extra work beyond its
    children here, per the spec's own worked example: "3 folded into 1 and delivered is worth
    3")."""

    def setUp(self):
        self.megas = {500: [501, 502, 503]}
        self.pr = _pr(900, "2026-09-24T10:00:00Z", "deadbeef", [500])

    def test_red_without_deploy_run(self):
        """RED: no deploy run has shipped the merge sha yet -- 0 resolved issues, not 3."""
        resolved = iph.resolved_issues([self.pr], deploy_runs=[], megas=self.megas,
                                        is_ancestor=_ancestor_factory(set()))
        self.assertEqual(resolved, [])

    def test_green_once_deployed(self):
        """GREEN: same PR, same command, now with a successful deploy run for its sha -- 3
        credited, all as mega_child (the mega's own number closed no additional standalone
        issue)."""
        deploys = [_deploy("deadbeef", "2026-09-24T11:00:00Z")]
        resolved = iph.resolved_issues([self.pr], deploy_runs=deploys, megas=self.megas,
                                        is_ancestor=_ancestor_factory({"deadbeef"}))
        self.assertEqual(len(resolved), 1)
        rec = resolved[0]
        self.assertEqual(rec["pr"], 900)
        self.assertEqual(rec["direct"], 0)
        self.assertEqual(rec["mega_child"], 3)
        self.assertEqual(rec["direct"] + rec["mega_child"], 3)

    def test_empty_mega_falls_back_to_one(self):
        """A mega with no children folded yet (just filed, nothing to fold) is worth 1 when
        closed -- one real thing resolved, not 0."""
        pr = _pr(900, "2026-09-24T10:00:00Z", "deadbeef", [500])
        megas = {500: []}
        deploys = [_deploy("deadbeef", "2026-09-24T11:00:00Z")]
        resolved = iph.resolved_issues([pr], deploy_runs=deploys, megas=megas,
                                        is_ancestor=_ancestor_factory({"deadbeef"}))
        self.assertEqual(resolved[0]["mega_child"], 1)

    def test_mega_plus_explicit_child_close_not_double_counted(self):
        """A PR closing the mega AND one of its own children in the same breath is still just
        the children's count -- the child close IS the mega's work, not separate from it."""
        pr = _pr(900, "2026-09-24T10:00:00Z", "deadbeef", [500, 501])
        deploys = [_deploy("deadbeef", "2026-09-24T11:00:00Z")]
        resolved = iph.resolved_issues([pr], deploy_runs=deploys, megas=self.megas,
                                        is_ancestor=_ancestor_factory({"deadbeef"}))
        self.assertEqual(resolved[0]["mega_child"], 3)


class DupeCloseTests(unittest.TestCase):
    """Fixture 2: an issue closed as dupe/stale/superseded has no merged-PR Closes-ref at all
    (GitHub's own `closingIssuesReferences` is empty for it; a bare "closed" state doesn't
    distinguish reasons) -- 0, always, regardless of deploy state."""

    def test_dupe_close_scores_zero(self):
        # No merged PR names this issue in its closing refs -- e.g. `gh issue close --reason
        # "not planned"`, exactly how issue_cluster.py's apply_plan() closes mega children
        # directly, or a human closing a duplicate by hand.
        merged_prs: list[dict] = []
        resolved = iph.resolved_issues(merged_prs, deploy_runs=[_deploy("x", "2026-09-24T00:00:00Z")],
                                        megas={}, is_ancestor=_ancestor_factory({"x"}))
        self.assertEqual(resolved, [])
        # It shows up only in the separate, muted closed-without-PR series.
        closed = [{"number": 777, "closedAt": "2026-09-24T09:00:00Z"}]
        no_pr = iph.closed_without_pr(closed, merged_prs)
        self.assertEqual(len(no_pr), 1)
        self.assertEqual(no_pr[0]["issue"], 777)


class UndeployedMergeTests(unittest.TestCase):
    """Fixture 3: a merged PR that closes a real issue but has not been deployed -- 0 until a
    successful deploy run ships its exact merge-commit sha (or an ancestor relationship covers
    it)."""

    def test_merged_undeployed_scores_zero(self):
        pr = _pr(42, "2026-09-24T08:00:00Z", "cafef00d", [123])
        resolved = iph.resolved_issues([pr], deploy_runs=[], megas={}, is_ancestor=_ancestor_factory(set()))
        self.assertEqual(resolved, [])

    def test_failed_deploy_run_does_not_count(self):
        """A deploy run that exists but FAILED never counts as shipping anything -- same filter
        scoreboard.live_merges() already applies."""
        pr = _pr(42, "2026-09-24T08:00:00Z", "cafef00d", [123])
        deploys = [_deploy("cafef00d", "2026-09-24T09:00:00Z", conclusion="failure")]
        resolved = iph.resolved_issues([pr], deploy_runs=deploys, megas={},
                                        is_ancestor=_ancestor_factory({"cafef00d"}))
        self.assertEqual(resolved, [])

    def test_merged_undeployed_then_deployed_later_credits_retroactively(self):
        pr = _pr(42, "2026-09-24T08:00:00Z", "cafef00d", [123])
        deploys = [_deploy("cafef00d", "2026-09-24T09:00:00Z")]
        resolved = iph.resolved_issues([pr], deploy_runs=deploys, megas={},
                                        is_ancestor=_ancestor_factory({"cafef00d"}))
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["direct"], 1)
        self.assertEqual(resolved[0]["mega_child"], 0)

    def test_deploy_of_unrelated_sha_does_not_credit_wrong_deploy(self):
        """A deploy run for an unrelated sha never counts -- deployed_by only credits a run
        where is_ancestor(merge_sha, run_sha) is true, and an honest is_ancestor fake never
        says yes for an unrelated sha."""
        pr = _pr(42, "2026-09-24T08:00:00Z", "cafef00d", [123])
        unrelated_deploy = [_deploy("some-other-sha", "2026-09-24T07:00:00Z")]
        resolved = iph.resolved_issues([pr], deploy_runs=unrelated_deploy, megas={},
                                        is_ancestor=_ancestor_factory({"cafef00d"}))
        self.assertEqual(resolved, [])


class HourlySeriesTests(unittest.TestCase):
    def test_buckets_direct_and_mega_separately_with_averages(self):
        resolved = [
            {"hour": "2026-09-24T10:00Z", "direct": 1, "mega_child": 0},
            {"hour": "2026-09-24T10:00Z", "direct": 0, "mega_child": 3},
            {"hour": "2026-09-24T09:00Z", "direct": 2, "mega_child": 0},
        ]
        no_pr = [{"hour": "2026-09-24T10:00Z"}]
        now = iph._parse_iso("2026-09-24T10:30:00Z")
        series = iph.hourly_series(resolved, no_pr, hours=3, now=now)
        self.assertEqual(series["hours"][-1], "2026-09-24T10:00Z")
        self.assertEqual(series["direct"][-1], 1)
        self.assertEqual(series["mega_child"][-1], 3)
        self.assertEqual(series["closed_without_pr"][-1], 1)
        self.assertEqual(series["direct"][-2], 2)
        self.assertGreater(series["current_24h_rate"], 0)


class DeployedByTests(unittest.TestCase):
    def test_picks_first_run_that_shipped_it_not_the_latest(self):
        deploys = [_deploy("b", "2026-09-24T02:00:00Z"), _deploy("a", "2026-09-24T01:00:00Z")]
        # Both runs "contain" the candidate (an honest ancestor check on a fast-forward history
        # would say yes to any run at or after the one that first shipped it) -- deployed_by
        # must still report the EARLIER ts.
        ts = iph.deployed_by("x", deploys, is_ancestor=lambda c, d: True)
        self.assertEqual(ts, iph._parse_iso("2026-09-24T01:00:00Z"))

    def test_no_deploys_returns_none(self):
        self.assertIsNone(iph.deployed_by("x", [], is_ancestor=lambda c, d: True))

    def test_no_sha_returns_none(self):
        self.assertIsNone(iph.deployed_by(None, [_deploy("a", "2026-09-24T01:00:00Z")], is_ancestor=lambda c, d: True))
        self.assertIsNone(iph.deployed_by("", [_deploy("a", "2026-09-24T01:00:00Z")], is_ancestor=lambda c, d: True))


class ChartSvgTests(unittest.TestCase):
    """chart_svg() is built server-side specifically so fleet_home.html's own hard byte cap
    (selftest.py: `len(page.encode()) < 48_000`) never has to pay for chart-rendering code."""

    def test_empty_series_returns_empty_string(self):
        self.assertEqual(iph.chart_svg({"hours": [], "direct": [], "mega_child": [],
                                        "closed_without_pr": [], "avg_24h": [], "avg_7d": []}), "")

    def test_renders_one_rect_per_nonzero_segment_and_two_polylines(self):
        series = iph.hourly_series(
            [{"hour": "2026-09-24T10:00Z", "direct": 1, "mega_child": 3}],
            [{"hour": "2026-09-24T10:00Z"}], hours=2, now=iph._parse_iso("2026-09-24T10:30:00Z"))
        svg = iph.chart_svg(series)
        self.assertTrue(svg.startswith("<svg"))
        self.assertEqual(svg.count("<rect"), 3)  # direct + mega_child + closed_without_pr, all nonzero
        self.assertEqual(svg.count("<polyline"), 2)  # avg_24h + avg_7d
        self.assertIn("var(--accent)", svg)
        self.assertIn("var(--ok)", svg)
        self.assertIn("var(--faint)", svg)


if __name__ == "__main__":
    unittest.main()
