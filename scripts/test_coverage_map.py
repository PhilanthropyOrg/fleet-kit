"""coverage_map.py: a production system with no watcher must be flagged, never scored green.

Reif 2026-09-24: the product repo's devops lane died in the migration to this fleet and nothing
noticed for weeks. These tests pin the three ways that happens -- an item nobody's mandate names,
a mandate on a member that stopped running, and a retired lane whose duties nobody inherited --
plus the real repo files: every `mandate.watches` id must exist in the checklist."""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coverage_map as cm  # noqa: E402

NOW = 1790277366
CHECKLIST = [
    {"id": "db.slow_queries", "area": "database", "needs": "db", "item": "slow queries"},
    {"id": "db.backups_pitr", "area": "database", "needs": "db", "item": "backups tested"},
    {"id": "pay.webhooks", "area": "payments", "needs": "payments", "item": "webhooks"},
    {"id": "jobs.success", "area": "jobs", "needs": "cron", "item": "cron exits"},
]
MEMBERS = [
    {"name": "nerd", "enabled": False, "mandate": {"watches": ["db.slow_queries"]}},   # on demand
    {"name": "old-watcher", "enabled": True, "mandate": {"watches": ["jobs.success"]}},
    {"name": "off", "enabled": False, "mandate": {"watches": ["db.backups_pitr"]}},     # disabled
]
RETIRED = [{"lane": "devops", "watched": ["db.slow_queries", "db.backups_pitr"]}]
INVENTORY = [{"id": "db:postgres", "kind": "db"}, {"id": "cron:ingest", "kind": "cron"},
             {"id": "queue:redis", "kind": "queue"}]
RUNS = {"nerd": NOW - 3600, "old-watcher": NOW - 10 * 86400}


def _by_id(m):
    return {it["id"]: it for it in m["items"]}


class CoverageDiff(unittest.TestCase):
    def setUp(self):
        self.m = cm.score(CHECKLIST, MEMBERS, RETIRED, INVENTORY, RUNS, now=NOW)
        self.items = _by_id(self.m)

    def test_system_with_no_watcher_is_flagged(self):
        self.assertEqual(self.items["db.backups_pitr"]["status"], "unwatched")

    def test_watched_item_names_its_watcher_and_last_run(self):
        self.assertEqual(self.items["db.slow_queries"]["status"], "watched")
        self.assertEqual(self.items["db.slow_queries"]["by"], ["nerd"])
        self.assertIn("1h ago", self.items["db.slow_queries"]["evidence"])

    def test_a_watcher_that_stopped_running_watches_nothing(self):
        self.assertEqual(self.items["jobs.success"]["status"], "unwatched")

    def test_absent_system_is_na_not_unwatched(self):
        self.assertEqual(self.items["pay.webhooks"]["status"], "n/a")

    def test_lane_that_died_in_a_migration_is_named(self):
        self.assertEqual(self.m["retired_gaps"], [{"lane": "devops", "id": "db.backups_pitr", "source": ""}])

    def test_inventory_system_of_an_uncovered_kind_is_unwatched(self):
        sysmap = {s["id"]: s for s in self.m["systems"]}
        self.assertEqual(sysmap["queue:redis"]["status"], "unwatched")
        self.assertEqual(sysmap["db:postgres"]["status"], "watched")
        self.assertEqual(sysmap["cron:ingest"]["status"], "unwatched")

    def test_hq_line_all_watched_vs_changed(self):
        ok = {"items": [{"id": "a", "status": "watched"}], "systems": []}
        self.assertEqual(cm.hq_line(ok, ok), "coverage map: all watched")
        line = cm.hq_line(ok, {"items": [{"id": "a", "status": "unwatched"}], "systems": []})
        self.assertIn("a: watched -> unwatched", line)
        self.assertIn("unwatched (1): a", line)


class RealRepoFiles(unittest.TestCase):
    def test_every_watches_id_exists_in_the_checklist(self):
        ids = {it["id"] for it in json.loads(cm.CHECKLIST.read_text())["items"]}
        for m in cm.load_members(cm.ROOT / "members"):
            for w in (m.get("mandate") or {}).get("watches") or []:
                self.assertIn(w, ids, f"{m['name']} watches unknown checklist id {w!r}")
        for r in json.loads(cm.RETIRED.read_text())["lanes"]:
            for w in r["watched"]:
                self.assertIn(w, ids, f"retired lane {r['lane']} names unknown id {w!r}")

    def test_prod_runtime_duties_have_a_live_watcher(self):
        m = cm.score(json.loads(cm.CHECKLIST.read_text())["items"], cm.load_members(cm.ROOT / "members"),
                     [], None, None)
        items = _by_id(m)
        for i in ("db.slow_queries", "db.connections", "db.timeouts", "jobs.ingest_freshness"):
            self.assertEqual(items[i]["status"], "watched", i)


if __name__ == "__main__":
    unittest.main()
