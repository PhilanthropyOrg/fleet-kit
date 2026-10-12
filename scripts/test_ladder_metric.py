#!/usr/bin/env python3
"""dumbledore can bet on user outcomes (Reif, 2026-10-12 mandate: "Dumbledore bets on outcomes
(back in HQ, checklist done, paid verified ...), not turns/cost").

Given north.py's hourly write read the product's after-claim ladder, When a prediction on
`ladder_orgs:<stage>` or `ladder_share:<stage>` is added or resolved, Then fleet_metrics.py
scores it from the snapshot nearest the due time, and says `unavailable` (None) when no
snapshot is near enough.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_metrics
import north
import predict

LADDER = {"days": 30, "stages": [
    {"key": "approved", "label": "Claim approved", "orgs": 272},
    {"key": "back_in_hq", "label": "Back in HQ", "orgs": 11, "untracked": True},
    {"key": "verified_badge", "label": "Verified badge", "orgs": 1}]}
T0 = 1_760_000_000.0


class LadderMetricTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"FLEET_LOG_DIR": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_in_catalog_with_direction(self):
        self.assertIn("ladder_orgs", fleet_metrics.CATALOG)
        self.assertEqual(fleet_metrics.direction("ladder_share:back_in_hq"), "higher")

    def test_orgs_and_share_from_nearest_snapshot(self):
        fleet_metrics.record_ladder(LADDER, T0)
        later = {"stages": [{"key": "approved", "orgs": 280}, {"key": "back_in_hq", "orgs": 28}]}
        fleet_metrics.record_ladder(later, T0 + 3600)
        self.assertEqual(fleet_metrics.compute("ladder_orgs:back_in_hq", [], T0 + 600), 11.0)
        self.assertEqual(fleet_metrics.compute("ladder_orgs:back_in_hq", [], T0 + 3500), 28.0)
        self.assertAlmostEqual(fleet_metrics.compute("ladder_share:back_in_hq", [], T0), 11 / 272)

    def test_unavailable_not_zero(self):
        self.assertIsNone(fleet_metrics.compute("ladder_orgs:back_in_hq", [], T0))  # no file
        fleet_metrics.record_ladder(LADDER, T0)
        self.assertIsNone(fleet_metrics.compute("ladder_orgs:back_in_hq", [], T0 + 3 * 3600))  # too far
        self.assertIsNone(fleet_metrics.compute("ladder_orgs:no_such_stage", [], T0))
        self.assertFalse(fleet_metrics.record_ladder({}, T0))  # empty feed writes nothing
        with self.assertRaises(ValueError):
            fleet_metrics.compute("ladder_orgs", [], T0)

    def test_north_write_records_a_snapshot(self):
        with mock.patch.object(north, "reif_prs", return_value=([], None)), \
             mock.patch.object(north, "fleet_prs", return_value={}), \
             mock.patch.object(north, "load_funnel", return_value=({"ladder": LADDER}, None)), \
             mock.patch.object(north, "LOG_DIR", Path(self.dir.name)), \
             mock.patch.object(north, "NORTH", Path(self.dir.name) / "NORTH.md"):
            self.assertEqual(north.main(["write"]), 0)
        snap = fleet_metrics.ladder_at(__import__("time").time())
        self.assertEqual(snap["back_in_hq"], 11)

    def test_predict_accepts_and_resolves_a_ladder_bet(self):
        fleet_metrics.parse("ladder_share:back_in_hq")  # predict.add calls this; must not raise
        fleet_metrics.record_ladder(LADDER, T0)
        self.assertEqual(fleet_metrics.compute("ladder_orgs:verified_badge", [], T0, predict.RESOLVE_WINDOW_H), 1.0)


if __name__ == "__main__":
    unittest.main()
