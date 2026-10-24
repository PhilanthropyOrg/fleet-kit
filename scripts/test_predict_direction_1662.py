#!/usr/bin/env python3
"""A bet only counts when the number moved the good way (fleet-kit#1662).

Given bet #95 on ci_minutes_per_day (lower is better) set its target 850 above its start 781.7,
When the actual came in at 1133 (minutes up 45%), Then it is scored a miss, not a hit; and a new
bet whose target sits on the bad side of its start is refused at add time.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict


class DirectionTests(unittest.TestCase):
    def test_bad_direction_bet_that_got_worse_is_a_miss(self):
        status, moved = predict.judge(781.7, 850.0, 1133.1, metric="ci_minutes_per_day")
        self.assertEqual(status, "miss")
        self.assertFalse(moved)

    def test_good_direction_bets_score_as_before(self):
        self.assertEqual(predict.judge(781.7, 700.0, 650.0, metric="ci_minutes_per_day")[0], "hit")
        self.assertEqual(predict.judge(781.7, 700.0, 750.0, metric="ci_minutes_per_day")[0], "miss")
        self.assertEqual(predict.judge(0.246, 0.255, 0.26, metric="ladder_share:orgverify_opened")[0], "hit")

    def test_no_direction_metric_keeps_baseline_rule(self):
        self.assertEqual(predict.judge(29.0, 22.0, 18.0, metric="runs_per_day:vp")[0], "hit")
        self.assertEqual(predict.judge(6.0, 9.0, 14.0, metric="status_per_day:ok:minion")[0], "hit")

    def test_add_refuses_target_on_bad_side(self):
        with self.assertRaises(ValueError):
            predict.make([], member="dumbledore", change="philanthropy#1", metric="ci_minutes_per_day",
                         target=850.0, baseline=781.7, by_hours=24, note="", now=0.0, runs=[], run_id=None)
        row = predict.make([], member="dumbledore", change="philanthropy#1", metric="ci_minutes_per_day",
                           target=700.0, baseline=781.7, by_hours=24, note="", now=0.0, runs=[], run_id=None)
        self.assertEqual(row["target"], 700.0)


if __name__ == "__main__":
    unittest.main()
