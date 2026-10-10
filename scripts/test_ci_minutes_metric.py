#!/usr/bin/env python3
"""dumbledore can bet on the mandate's own number (Reif, 2026-10-08: "max CI mins used today 500").

Given ci_minutes.py has read the billing API, When a prediction on `ci_minutes_per_day` is
added or resolved, Then fleet_metrics.py scores the Actions minutes used in the 24h ending at
due, and says `unavailable` (None) when the cache does not cover that window.
"""

from __future__ import annotations

import calendar
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ci_minutes
import fleet_metrics
import predict


def t(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M")))


def item(day: str, q: float) -> dict:
    return {"product": "actions", "unitType": "Minutes", "date": f"{day}T00:00:00Z", "quantity": q}


class CiMinutesMetricTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"FLEET_LOG_DIR": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def write(self, ts: float, days: dict) -> None:
        Path(self.dir.name, "ci_minutes.json").write_text(json.dumps({"ts": ts, "days": days}))

    def test_metric_is_listed_and_lower_is_better(self):
        self.assertEqual(fleet_metrics.parse("ci_minutes_per_day"), ("ci_minutes_per_day", []))
        self.assertEqual(fleet_metrics.direction("ci_minutes_per_day"), "lower")

    def test_scores_the_24h_ending_at_due(self):
        # 2,400 on Oct 6 and 7; 1,200 by noon Oct 8 (the read time).
        self.write(t("2026-10-08 12:00"), {"2026-10-06": 2400, "2026-10-07": 2400, "2026-10-08": 1200})
        # noon Oct 7 -> noon Oct 8: half of Oct 7 (1,200) + the 1,200 so far on Oct 8.
        self.assertAlmostEqual(fleet_metrics.compute("ci_minutes_per_day", [], t("2026-10-08 12:00")), 2400.0)
        # midnight to midnight is exactly one day's total.
        self.assertAlmostEqual(fleet_metrics.compute("ci_minutes_per_day", [], t("2026-10-08 00:00")), 2400.0)

    def test_unavailable_when_the_cache_does_not_cover_the_window(self):
        self.assertIsNone(fleet_metrics.compute("ci_minutes_per_day", [], t("2026-10-08 12:00")))  # no file
        self.write(t("2026-10-08 12:00"), {"2026-10-08": 1200})
        self.assertIsNone(fleet_metrics.compute("ci_minutes_per_day", [], t("2026-10-08 12:00")))  # starts too late
        self.write(t("2026-10-08 12:00"), {"2026-10-07": 2400, "2026-10-08": 1200})
        self.assertIsNone(fleet_metrics.compute("ci_minutes_per_day", [], t("2026-10-08 14:00")))  # read too early

    def test_ci_minutes_saves_and_merges_the_cache(self):
        with mock.patch.object(ci_minutes, "fetch", return_value=[item("2026-10-07", 2000), item("2026-10-07", 400),
                                                                  item("2026-10-08", 100)]):
            ci_minutes.main(["ci_minutes.py", "Org"])
        first = json.loads(Path(self.dir.name, "ci_minutes.json").read_text())
        self.assertEqual(first["days"], {"2026-10-07": 2400.0, "2026-10-08": 100.0})
        with mock.patch.object(ci_minutes, "fetch", return_value=[item("2026-10-08", 300)]):
            ci_minutes.main(["ci_minutes.py", "Org"])
        second = json.loads(Path(self.dir.name, "ci_minutes.json").read_text())
        self.assertEqual(second["days"], {"2026-10-07": 2400.0, "2026-10-08": 300.0})
        self.assertGreaterEqual(second["ts"], first["ts"])

    def test_a_mandate_bet_resolves_hit_or_miss(self):
        rows = [predict.make([], member="dumbledore", change="fleet-kit#1623", metric="ci_minutes_per_day",
                             target=500, baseline=2737, by_hours=24, note="", now=t("2026-10-09 00:00"),
                             runs=[], run_id=None)]
        self.write(t("2026-10-10 02:00"), {"2026-10-08": 2737, "2026-10-09": 480, "2026-10-10": 20})
        (r,) = predict.resolve(rows, [], t("2026-10-10 02:13"))
        self.assertEqual((r["status"], r["actual"]), ("hit", 480.0))


class BySource(unittest.TestCase):
    """Given today's runs, When `--by` splits them, Then deploys, fleet branches and the rest
    are named apart, so the over-limit report says whose minutes they were."""

    def test_buckets(self):
        def r(name: str, branch: str, mins: int) -> dict:
            return {"name": name, "head_branch": branch, "run_started_at": "2026-10-09T01:00:00Z",
                    "updated_at": f"2026-10-09T01:{mins:02d}:00Z"}

        got = ci_minutes.by_source({
            "O/philanthropy": [r("DEPLOY", "main", 25), r("CI", "member/minion-x", 10),
                               r("CI", "hq", 7), r("CI", "dependabot/uv", 3), r("OPS", "main", 1)],
            "O/fleet-kit": [r("CI", "dumbledore/y", 2)],
        })
        self.assertEqual(got, {"deploy": 25, "fleet": 12, "other-branches": 7,
                               "dependabot": 3, "main-ops": 1})

    def test_jobs_not_wall_time(self):
        # A deploy that waits 45 min on the production timer bills only its job: 12 min, not 57.
        run = {"run_started_at": "2026-10-09T01:00:00Z", "updated_at": "2026-10-09T01:57:00Z",
               "jobs": [{"started_at": "2026-10-09T01:45:00Z", "completed_at": "2026-10-09T01:56:10Z"},
                        {"started_at": "2026-10-09T01:56:20Z", "completed_at": "2026-10-09T01:56:20Z"},
                        {"started_at": None, "completed_at": None}]}
        self.assertEqual(ci_minutes.wall_minutes(run), 12.0)

    def test_bad_timestamps_count_zero(self):
        self.assertEqual(ci_minutes.wall_minutes({"run_started_at": None}), 0.0)


if __name__ == "__main__":
    unittest.main()
