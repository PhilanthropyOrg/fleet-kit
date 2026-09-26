"""gh#8212, Reif 2026-09-26: "why this large weird graph at the top, just keep what we have as
tiles, but make sure they are updated." Tiles only; one tile per quantity; a failed gh read
keeps the last good value instead of reading 0; every tile carries as_of + cadence_s; and two
stall tiles: PRs open > 4h and runs stuck at `started` past their timeout.

Run: python3 scripts/test_fleet_home_tiles_8212.py
"""
import datetime
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Tiles8212(unittest.TestCase):
    def _snap(self, gh, run_rows=()):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old = {k: os.environ.get(k) for k in ("FLEET_DB_PATH", "FLEET_LOG_DIR", "FLEET_ENV_FILE", "ACCOUNT_POOL_STATE_FILE")}
            os.environ["FLEET_LOG_DIR"] = str(tmp)
            os.environ["FLEET_DB_PATH"] = str(tmp / "fleet.db")
            (tmp / "fleet.env").write_text("FLEET_ACCOUNTS=a\n")
            os.environ["FLEET_ENV_FILE"] = str(tmp / "fleet.env")
            os.environ["ACCOUNT_POOL_STATE_FILE"] = str(tmp / "nope.state")
            (tmp / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in run_rows))
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                fvs = importlib.import_module("fleet_view_server")
                fvs.ENV_FILE = tmp / "fleet.env"
                fvs.RUNS_FILE = tmp / "runs.jsonl"
                import fleet_db
                fleet_db.DB_FILE = tmp / "fleet.db"
                fvs.STATE.gh = gh
                fvs.STATE.runs = []
                fvs._TTL_CACHE.clear()
                fvs._gh = lambda *a, **k: ""
                fvs._fleet_prs_opened = lambda: []
                return fvs, {m["id"]: m for m in fvs.metrics_snapshot()["metrics"]}
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_failed_backlog_read_keeps_last_good_value(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        fvs = importlib.import_module("fleet_view_server")
        prev = {"issues": [{"number": 1}] * 448, "issues_truncated": False, "issues_at": 100.0,
                "prs": [{"number": 9}], "prs_at": 100.0, "merged": [], "merged_at": 100.0}
        new = {"issues": [], "issues_truncated": False, "issues_ok": False,
               "prs": [], "prs_ok": True, "merged": [], "merged_ok": True, "polled_at": 200.0}
        out = fvs.merge_gh_poll(prev, new)
        self.assertEqual(len(out["issues"]), 448, "a timed-out backlog read must not publish 0")
        self.assertEqual(out["issues_at"], 100.0, "the carried value keeps its own, older as-of")
        self.assertEqual(out["prs"], [], "a good read of an empty list is a real 0")
        self.assertEqual(out["prs_at"], 200.0)

    def test_stall_tiles_and_freshness(self):
        now = time.time()
        prs = [{"number": 11, "isDraft": False, "createdAt": iso(now - 7 * 3600)},
               {"number": 12, "isDraft": False, "createdAt": iso(now - 5 * 3600)},
               {"number": 13, "isDraft": True, "createdAt": iso(now - 600)}]
        gh = {"prs": prs, "prs_at": now - 30, "issues": [], "issues_at": now - 30, "merged": [], "poll_s": 40}
        runs = [{"run_id": "minion-a", "member": "minion", "status": "started", "ts": now - 7000, "timeout_s": 5400},
                {"run_id": "gru-b", "member": "gru", "status": "started", "ts": now - 60, "timeout_s": 7200},
                {"run_id": "fixer-c", "member": "the-fixer", "status": "started", "ts": now - 9000, "timeout_s": 1800},
                {"run_id": "fixer-c", "member": "the-fixer", "status": "ok", "ts": now - 8000}]
        _, by = self._snap(gh, runs)
        t = by["fleet.prs_open_over_4h"]
        self.assertEqual(t["value"], 2)
        self.assertIn("#11", t["sub"])
        self.assertIn("7h", t["sub"])
        self.assertTrue(t["bad"])
        s = by["fleet.runs_stuck"]
        self.assertEqual(s["value"], 1, "only minion-a is past its timeout with no ending")
        self.assertIn("minion", s["sub"])
        self.assertIn("1 running", s["sub"])
        self.assertEqual(by["fleet.msgs_open"]["value"], 0,
                         "the msgs tile must read on the snapshot's own connection, not lock against it")
        for mid, m in by.items():
            self.assertIn("as_of", m, mid)
            self.assertIn("cadence_s", m, mid)
        self.assertFalse(by["fleet.prs_open"]["stale"])
        self.assertLessEqual(now - by["fleet.prs_open"]["as_of"], 2 * by["fleet.prs_open"]["cadence_s"])

    def test_old_gh_read_is_stale(self):
        now = time.time()
        _, by = self._snap({"prs": [], "prs_at": now - 3600, "issues": [], "issues_at": now - 3600,
                            "merged": [], "poll_s": 40})
        self.assertTrue(by["fleet.backlog_open"]["stale"], "an hour-old poll is past 2x a ~1-min cadence")

    def test_one_tile_per_quantity_and_no_top_chart(self):
        ids = [m["id"] for m in json.loads((ROOT / "scripts" / "metrics.json").read_text())["metrics"]]
        for gone in ("fleet.issues_resolved_per_hour", "fleet.issues_resolved_7d", "fleet.backlog_trend"):
            self.assertNotIn(gone, ids)
        for want in ("fleet.issues_resolved_24h", "fleet.prs_open_over_4h", "fleet.runs_stuck"):
            self.assertIn(want, ids)
        page = (ROOT / "scripts" / "fleet_home.html").read_text()
        self.assertNotIn("/api/iph", page, "the home page no longer draws the Issues/hr chart")
        self.assertNotIn('id="ic"', page)
        css = page.split(".stat .s{", 1)[1].split("}", 1)[0]
        self.assertNotIn("ellipsis", css, "tile captions wrap, never truncate")
        self.assertIn("asOfHtml(m)", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
