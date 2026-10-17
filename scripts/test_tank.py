"""The fleet uses the whole week: a 10-point budget gap raises the builder cap, and shows."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_meter  # noqa: E402
import fleet_metrics  # noqa: E402
import tank  # noqa: E402

NOW = 1792215000.0  # 2026-10-17 05:30Z
PHIL = {"name": "phil", "week_left": 87, "elapsed": 84, "walled": False}   # +71 points
REIF = {"name": "reif", "week_left": 51, "elapsed": 52, "walled": False}   # +3 points
TGP = {"name": "tgp", "error": "no reading: long-lived setup token"}
PRS = [{"title": "Jobs leaves hospitals out", "headRefName": "g/j6", "mergedAt": "2026-10-17T03:00:00Z"},
       {"title": "OrgVerify step 2", "headRefName": "g/ov2", "mergedAt": "2026-10-16T12:00:00Z"},
       {"title": "HQ one Messages row", "headRefName": "g/fx53", "mergedAt": "2026-10-15T01:00:00Z"}]


class Tank(unittest.TestCase):
    def test_a_ten_point_gap_is_one_more_builder_start(self):
        near = {"name": "x", "week_left": 30, "elapsed": 80, "walled": False}  # 30 - 20 = +10
        self.assertLess(round(account_meter.pace_multiplier(near)), 2)       # pace alone: still 1
        self.assertEqual(account_meter.paced_hour_cap(1, [near]), 2)
        self.assertEqual(account_meter.paced_hour_cap(1, [REIF]), 1)

    def test_section_shows_each_account_the_cap_and_merged_by_priority(self):
        text = "\n".join(tank.lines([PHIL, REIF, TGP], 1, 2, tank.merged_counts(PRS, NOW)))
        self.assertIn("phil: 87% of the week's budget left, 16% of the week left (+71 points)", text)
        self.assertIn("tgp: unreadable", text)
        self.assertIn("builder: 1 started this hour, cap 2", text)
        self.assertIn("PRs merged 24h: 2 (jobs 1, orgverify 1, hq 0, audit 0)", text)
        self.assertIn("RULE: phil", text)
        self.assertIn("`merged_prs_per_day:jobs`", text)
        self.assertNotIn("UNREADABLE METER", text)

    def test_no_readable_meter_is_the_headline(self):
        text = "\n".join(tank.lines([TGP], 0, 1, {}))
        self.assertIn("UNREADABLE METER", text)
        self.assertNotIn("RULE:", text)

    def test_merged_prs_metric_reads_the_snapshot(self):
        os.environ["FLEET_LOG_DIR"] = tempfile.mkdtemp()
        tank.record(tank.merged_counts(PRS, NOW), NOW)
        self.assertEqual(fleet_metrics.compute("merged_prs_per_day:jobs", [], NOW + 60), 1.0)
        self.assertEqual(fleet_metrics.compute("merged_prs_per_day", [], NOW + 60), 2.0)
        self.assertEqual(fleet_metrics.direction("merged_prs_per_day:jobs"), "higher")


if __name__ == "__main__":
    unittest.main()
