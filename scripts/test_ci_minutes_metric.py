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


if __name__ == "__main__":
    unittest.main()
