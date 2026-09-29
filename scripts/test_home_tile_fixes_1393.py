"""gh#1393 follow-up (Reif, from home-desktop-1440.jpg): seven tile-level misses in the first
pass at fk#1393's redesign.

RED without the fix / GREEN with it, per case:
1. Claim completion showed "90.7" (no unit) -- must read "91%", sub "4 pending" (no median-age
   clause). Value/sub asserted here; the "%" rendering itself is in fleet_home.html's fmtValue().
2. "Reif-priority throughput 0.67" was a rate -- renamed "Your requests", value is a 6h COUNT,
   sub is "N waiting" (no rate, no queue-empty prose).
3. "Gate drops 51" was gru's 6h intake-gate FLOW -- renamed "Needs spec", value is the STOCK of
   open fleet:needs-spec issues (the ones actually waiting on marie right now).
4. Fleet messages' sub ran past 100 chars (every member's count + the oldest's full detail +
   the 24h answered count). Home's caption budget is ~50 chars.
5. Issues resolved's sub ran to ~85 chars for the same reason. Same budget.
6. Claude accounts' sub must name which account is running now, read from account_pool.sh's
   own "account=<name> call succeeded" log line.
7. The number's sub adds a pace clause: "+60 7d · need N/wk to hit 3,000", derived from the
   OKR's own target value and deadline (target.by) -- the hero's larger type is a fleet_home.html
   CSS rule, asserted by source string here.

Run: python3 scripts/test_home_tile_fixes_1393.py
"""
import datetime
import importlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class HomeTileFixes1393(unittest.TestCase):
    def _snap(self, gh=None, number_payload=None, account_log=None, msg_summary=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old = {k: os.environ.get(k) for k in ("FLEET_DB_PATH", "FLEET_LOG_DIR", "FLEET_ENV_FILE", "ACCOUNT_POOL_STATE_FILE")}
            os.environ["FLEET_LOG_DIR"] = str(tmp)
            os.environ["FLEET_DB_PATH"] = str(tmp / "fleet.db")
            (tmp / "fleet.env").write_text("FLEET_ACCOUNTS=philanthropy\n")
            os.environ["FLEET_ENV_FILE"] = str(tmp / "fleet.env")
            os.environ["ACCOUNT_POOL_STATE_FILE"] = str(tmp / "nope.state")
            if account_log is not None:
                (tmp / "account-pool.log").write_text(account_log)
            old_number_read = sys.modules.get("number_read")
            old_summary = None
            fvs = None
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                fvs = importlib.import_module("fleet_view_server")
                old_summary = fvs.fleet_msg.summary
                fvs.ENV_FILE = tmp / "fleet.env"
                fvs.LOG_DIR = tmp  # _active_account() reads LOG_DIR/account-pool.log
                import fleet_db
                fleet_db.DB_FILE = tmp / "fleet.db"
                fvs.STATE.gh = gh if gh is not None else {"prs": [], "issues": [], "issues_truncated": False, "merged": []}
                fvs.STATE.runs = []
                fvs._TTL_CACHE.clear()
                fvs._gh = lambda *a, **k: ""
                fvs._fleet_prs_opened = lambda: []

                class FakeNR:
                    @staticmethod
                    def read_current():
                        if number_payload is None:
                            return {"present": False}
                        return {"present": True, "stale": False, "payload": number_payload}
                sys.modules["number_read"] = FakeNR

                if msg_summary is not None:
                    fvs.fleet_msg.summary = staticmethod(lambda db: msg_summary)

                return fvs, {m["id"]: m for m in fvs.metrics_snapshot()["metrics"]}
            finally:
                if fvs is not None and old_summary is not None:
                    fvs.fleet_msg.summary = old_summary
                if old_number_read is None:
                    sys.modules.pop("number_read", None)
                else:
                    sys.modules["number_read"] = old_number_read
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    NUMBER_PAYLOAD = {
        "fetched_at": 1000,
        "number": {"value": 201, "unit": "claims", "delta_7d": 60},
        "target": {"value": 3000, "by": "2026-12-31"},
        "kr1": {"value": 70, "unit": "claims", "completion_rate_pct": 90.7, "pending": 4, "median_pending_age_days": 0.1},
    }

    # 1. Claim completion
    def test_claim_completion_sub_is_pending_count_only(self):
        _, by = self._snap(number_payload=self.NUMBER_PAYLOAD)
        conv = by["okr.conversion"]
        self.assertEqual(conv["value"], 90.7)
        self.assertEqual(conv["sub"], "4 pending", conv["sub"])
        self.assertNotIn("median", conv["sub"])

    def test_home_html_renders_percent_unit_as_a_rounded_percent(self):
        page = (ROOT / "scripts" / "fleet_home.html").read_text()
        self.assertIn("Math.round(m.value)", page)
        self.assertIn("'%'", page)

    # 2. Your requests
    def test_your_requests_is_registered_as_a_count_not_a_rate(self):
        ids = {m["id"] for m in json.loads((ROOT / "scripts" / "metrics.json").read_text())["metrics"]}
        reg = {m["id"]: m for m in json.loads((ROOT / "scripts" / "metrics.json").read_text())["metrics"]}
        self.assertIn("fleet.reif_priority_throughput_6h", ids)
        self.assertEqual(reg["fleet.reif_priority_throughput_6h"]["label"], "Your requests")
        src = (ROOT / "scripts" / "fleet_view_server.py").read_text()
        self.assertIn('"value": reif_done6', src)
        self.assertNotIn('"value": round(reif_rate6, 2)', src)

    def test_your_requests_sub_is_just_the_waiting_count(self):
        now = time.time()
        gh = {"prs": [], "issues": [
            {"number": 1, "labels": [{"name": "fleet:reif-priority"}]},
            {"number": 2, "labels": [{"name": "fleet:reif-priority"}]},
            {"number": 3, "labels": []},
        ], "issues_truncated": False, "merged": [], "issues_at": now, "prs_at": now, "merged_at": now}
        _, by = self._snap(gh=gh)
        sub = by["fleet.reif_priority_throughput_6h"]["sub"]
        # unavailable (no deploy history in this harness) either way, but never a rate string
        self.assertNotIn("/hr", sub)
        self.assertNotIn("queue has open items", sub)
        self.assertNotIn("queue empty", sub)

    # 3. Needs spec
    def test_needs_spec_is_registered_as_a_stock_not_the_6h_flow(self):
        reg = {m["id"]: m for m in json.loads((ROOT / "scripts" / "metrics.json").read_text())["metrics"]}
        self.assertEqual(reg["fleet.gate_drops_6h"]["label"], "Needs spec")
        src = (ROOT / "scripts" / "fleet_view_server.py").read_text()
        self.assertIn('needs_spec_n = (gh.get("needs_spec")', src)
        self.assertNotIn('"value": gd["items"]', src)

    def test_needs_spec_value_is_the_open_label_count(self):
        now = time.time()
        gh = {"prs": [], "issues": [], "issues_truncated": False, "merged": [],
              "issues_at": now, "prs_at": now, "merged_at": now, "needs_spec": {"count": 45}}
        _, by = self._snap(gh=gh)
        t = by["fleet.gate_drops_6h"]
        self.assertEqual(t["value"], 45)
        self.assertIn("45", t["sub"])
        self.assertTrue(t["bad"])

    # 4. Fleet messages caption budget
    def test_fleet_messages_sub_fits_the_caption_budget(self):
        summary = {"open": 12, "escalated": 0, "closed_24h": 129,
                   "by_member": {"dumbledore": 2, "jefe": 1, "marie": 8, "vp": 1},
                   "oldest": {"id": 220, "to": "marie", "kind": "gate-drop", "age_s": 7 * 3600 + 38 * 60,
                              "escalated_to": None}}
        _, by = self._snap(msg_summary=summary)
        sub = by["fleet.msgs_open"]["sub"]
        self.assertLessEqual(len(sub), 50, sub)
        self.assertIn("12", sub)
        self.assertIn("marie", sub)

    # 5. Issues resolved caption budget
    def test_issues_resolved_sub_fits_the_caption_budget(self):
        src = (ROOT / "scripts" / "fleet_view_server.py").read_text()
        # the long-form is documented as removed; the short form is what ships
        self.assertNotIn('f"merged PR + deployed, 24h ·', src)
        self.assertIn('f"{resolved7d} in 7d · {resolved7d / 7:.0f}/day"', src)

    # 6. Claude accounts names the active account
    def test_claude_accounts_names_the_active_account_from_the_pool_log(self):
        now_str = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        log = (f"[{now_str}] account_pool: account=tgp call succeeded\n"
               f"[{now_str}] account_pool: account=philanthropy call succeeded\n")
        _, by = self._snap(account_log=log)
        self.assertIn("running on philanthropy", by["fleet.accounts_live"]["sub"])

    def test_claude_accounts_falls_back_cleanly_with_no_log(self):
        _, by = self._snap()
        self.assertNotIn("running on", by["fleet.accounts_live"]["sub"])

    # 7. The number: pace + hero styling
    def test_the_number_sub_carries_a_weekly_pace_to_the_deadline(self):
        _, by = self._snap(number_payload=self.NUMBER_PAYLOAD)
        sub = by["okr.verified_claims"]["sub"]
        # The pace depends on today's date (weeks left to 2026-12-31): pin the shape, and the
        # number to the same formula, not a constant that went stale a day after it was written.
        m = re.fullmatch(r"\+60 7d · need (\d+)/wk to hit 3,000", sub)
        self.assertIsNotNone(m, sub)
        left = (datetime.datetime(2026, 12, 31, tzinfo=datetime.timezone.utc)
                - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / (7 * 86400)
        self.assertLessEqual(abs(int(m.group(1)) - (3000 - 201) / left), 1, sub)

    def test_the_number_falls_back_to_delta_only_with_no_deadline(self):
        payload = json.loads(json.dumps(self.NUMBER_PAYLOAD))
        del payload["target"]["by"]
        _, by = self._snap(number_payload=payload)
        self.assertEqual(by["okr.verified_claims"]["sub"], "+60 7d")

    def test_home_html_gives_the_number_tile_a_larger_value(self):
        page = (ROOT / "scripts" / "fleet_home.html").read_text()
        self.assertIn('data-metric-id="okr.verified_claims"', page)
        self.assertIn('okr.verified_claims"] .v{font-size:', page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
