"""dumbledore bets on what each company is for: the product numbers read, and the 2-of-3 rule holds."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_metrics  # noqa: E402
import predict  # noqa: E402
import product_metrics as pm  # noqa: E402

NOW = 1792215000.0  # 2026-10-17 05:30Z
KPI = {
    "claims": {"verified_orgs": 3, "funnel": {"totals_24h": {"claim_page": 32, "submitted": 12}}},
    "money": {"recent": [
        {"t": "2026-10-16T20:25:17Z", "kind": "checkout", "status": "complete", "tier": "990Scout+ Report"},
        {"t": "2026-10-16T20:25:17Z", "kind": "checkout", "status": "open", "tier": "990Scout+ Report"},
        {"t": "2026-10-01T20:25:17Z", "kind": "checkout", "status": "complete", "tier": "990Scout+ Report"},
        {"t": "2026-10-16T20:25:17Z", "kind": "checkout", "status": "complete", "tier": "Verified Person"}]},
    "jobs": {"applications": {"last_7d": 1}, "search": {"clicks_7d": 93}},
}


class ProductMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["FLEET_LOG_DIR"], os.environ["FLEET_G_SHARE"] = str(self.tmp), str(self.tmp)

    def test_philanthropy_numbers_from_kpi(self):
        v = pm.from_kpi(KPI, NOW)
        self.assertEqual(v, {"po.paid_verified_orgs": 3, "po.claims_started_24h": 32,
                             "po.claims_finished_24h": 12, "po.scout_plus_sales_7d": 1,
                             "po.job_applies_7d": 1, "po.jobs_from_google_7d": 93})

    def test_real_browser_funnel_wins_once_it_exists(self):
        day = {"google": 10, "second_page": 4, "apply_out": 2, "applied_here": 1, "crawler_hit": False}
        kpi = {"jobs": {"funnel": {"real": [day] * 7 + [{"google": 99}]}}}  # last = today, partial
        v = pm.from_kpi(kpi, NOW)
        self.assertEqual((v["po.job_applies_7d"], v["po.jobs_from_google_7d"]), (21, 70))
        self.assertAlmostEqual(v["po.jobs_second_page_pct"], 40.0)

    def test_drops_read_when_fresh_and_scored_by_fleet_metrics(self):
        (self.tmp / "dyw-metrics.json").write_text(json.dumps({"ts": NOW - 3600, "applicants_per_day": 14}))
        (self.tmp / "gymfleet-metrics.json").write_text(json.dumps({"ts": NOW - 40 * 3600, "members": 9}))
        pm.record(KPI, NOW)
        self.assertEqual(fleet_metrics.compute("product:dyw.applicants_per_day", [], NOW + 60), 14.0)
        self.assertIsNone(fleet_metrics.compute("product:gym.members", [], NOW + 60))  # stale drop
        self.assertEqual(fleet_metrics.compute("product:po.paid_verified_orgs", [], NOW + 60), 3.0)
        self.assertEqual(fleet_metrics.direction("product:dyw.meta_spend_per_applicant"), "lower")
        self.assertEqual(fleet_metrics.direction("product:po.job_applies_7d"), "higher")

    def test_a_third_plumbing_bet_in_a_row_is_refused(self):
        rows = [{"member": "dumbledore", "metric": "avg_turns:gru"},
                {"member": "dumbledore", "metric": "quiet_rate:vp"}]
        self.assertIn("refused", predict.plumbing_streak_error(rows, "dumbledore", "runs_per_day", ""))
        self.assertIsNone(predict.plumbing_streak_error(rows, "dumbledore", "product:po.job_applies_7d", ""))
        self.assertIsNone(predict.plumbing_streak_error(rows, "dumbledore", "ci_minutes_per_day",
                                                        "CI 640/500 min today"))
        self.assertIsNone(predict.plumbing_streak_error(rows, "g", "avg_turns:gru", ""))
        rows[0]["metric"] = "ladder_share:back_in_hq"
        self.assertIsNone(predict.plumbing_streak_error(rows, "dumbledore", "avg_turns:gru", ""))


if __name__ == "__main__":
    unittest.main()
