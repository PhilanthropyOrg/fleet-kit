"""Reif 2026-09-19: "PRs last 24 hours on a graph would be good. And spawns last 24 hours
(minion). If spawns > PRs not good." Two native 24h tiles; the spawn tile carries bad=True
when minion passes started exceed fleet PRs opened in the same 24h."""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SpawnsVsPrs(unittest.TestCase):
    def _snap(self, runs, prs):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old = {k: os.environ.get(k) for k in ("FLEET_DB_PATH", "FLEET_LOG_DIR", "FLEET_ENV_FILE", "ACCOUNT_POOL_STATE_FILE")}
            os.environ["FLEET_LOG_DIR"] = str(tmp)
            os.environ["FLEET_DB_PATH"] = str(tmp / "fleet.db")
            (tmp / "fleet.env").write_text("FLEET_ACCOUNTS=a\n")
            os.environ["FLEET_ENV_FILE"] = str(tmp / "fleet.env")
            os.environ["ACCOUNT_POOL_STATE_FILE"] = str(tmp / "nope.state")
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                fvs = importlib.import_module("fleet_view_server")
                fvs.ENV_FILE = tmp / "fleet.env"
                import fleet_db
                fleet_db.DB_FILE = tmp / "fleet.db"
                fvs.STATE.gh = {"prs": [], "issues": [], "merged": []}
                fvs.STATE.runs = runs
                fvs._TTL_CACHE.clear()
                fvs._gh = lambda *a, **k: ""
                fvs._fleet_prs_opened = lambda: prs
                return {m["id"]: m for m in fvs.metrics_snapshot()["metrics"]}
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_registered(self):
        ids = [m["id"] for m in json.loads((ROOT / "scripts" / "metrics.json").read_text())["metrics"]]
        self.assertIn("fleet.minion_spawns_per_hour", ids)
        self.assertIn("fleet.prs_opened_per_hour", ids)
        self.assertIn("if (m.bad) cls = 'bad';", (ROOT / "scripts" / "fleet_home.html").read_text())

    def test_more_spawns_than_prs_is_bad(self):
        now = time.time()
        iso = lambda age: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age))
        runs = [{"member": "minion", "status": "started", "ts": now - 600},
                {"member": "minion", "status": "started", "ts": now - 7200},
                {"member": "minion", "status": "ok", "ts": now - 6000},        # an end record, not a spawn
                {"member": "gru", "status": "started", "ts": now - 300},       # not minion
                {"member": "minion", "status": "started", "ts": now - 30 * 3600}]  # outside 24h
        by = self._snap(runs, [{"createdAt": iso(1800)}, {"createdAt": iso(40 * 3600)}])
        sp, pr = by["fleet.minion_spawns_per_hour"], by["fleet.prs_opened_per_hour"]
        self.assertEqual(sp["value"], 2)
        self.assertEqual(pr["value"], 1)
        self.assertTrue(sp["bad"])
        self.assertEqual(len(sp["series"]), 24)
        self.assertEqual(sum(p["value"] for p in pr["series"]), 1)

    def test_prs_keep_up_is_not_bad(self):
        now = time.time()
        iso = lambda age: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age))
        by = self._snap([{"member": "minion", "status": "started", "ts": now - 600}],
                        [{"createdAt": iso(1800)}, {"createdAt": iso(3600)}])
        self.assertFalse(by["fleet.minion_spawns_per_hour"]["bad"])


if __name__ == "__main__":
    unittest.main()
