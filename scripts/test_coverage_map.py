"""coverage_map.py: the production-readiness audit must FAIL, never score green, when a checklist
item has no live owner, its owner's check stopped running, or a data inflow stopped arriving.

Reif 2026-09-24: "keep that list, and have an agent on every one of those things. I'm especially
interested in data inflows." The product repo's devops lane died in the migration to this fleet
and nothing noticed for weeks. These tests pin each way that happens plus the real repo files:
every checklist item names exactly one existing member, every inflow an existing owner."""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coverage_map as cm  # noqa: E402
import inflows  # noqa: E402

NOW = 1790277366
CHECKLIST = [
    {"id": "db.slow_queries", "area": "database", "needs": "db", "item": "slow queries", "owner": "nerd", "cadence_h": 6},
    {"id": "db.backups_pitr", "area": "database", "needs": "db", "item": "backups tested", "owner": "nerd", "cadence_h": 72,
     "evidence": "backups.txt"},
    {"id": "pay.webhooks", "area": "payments", "needs": "payments", "item": "webhooks", "owner": "nerd", "cadence_h": 72},
    {"id": "jobs.success", "area": "jobs", "needs": "cron", "item": "cron exits", "owner": "old-watcher", "cadence_h": 24},
]
MEMBERS = [
    {"name": "nerd", "enabled": False},   # on demand
    {"name": "old-watcher", "enabled": True},
    {"name": "off", "enabled": False},
]
RETIRED = [{"lane": "devops", "watched": ["db.slow_queries", "jobs.success"]}]
INVENTORY = [{"id": "db:postgres", "kind": "db"}, {"id": "cron:ingest", "kind": "cron"},
             {"id": "queue:redis", "kind": "queue"}]
RUNS = {"nerd": NOW - 3600, "old-watcher": NOW - 10 * 86400}
EVIDENCE = {"backups.txt": NOW - 60}


def _by_id(m):
    return {it["id"]: it for it in m["items"]}


def _score(checklist=CHECKLIST, members=MEMBERS, runs=RUNS):
    return cm.score(checklist, members, RETIRED, INVENTORY, runs, now=NOW, evidence_mtimes=EVIDENCE)


class Ownership(unittest.TestCase):
    def setUp(self):
        self.m = _score()
        self.items = _by_id(self.m)

    def test_owned_fresh_item_is_watched_and_names_owner_and_last_run(self):
        self.assertEqual(self.items["db.slow_queries"]["status"], "watched")
        self.assertEqual(self.items["db.slow_queries"]["by"], ["nerd"])
        self.assertIn("1h ago", self.items["db.slow_queries"]["evidence"])

    def test_evidence_file_mtime_is_the_last_run(self):
        self.assertEqual(self.items["db.backups_pitr"]["status"], "watched")
        self.assertIn("backups.txt", self.items["db.backups_pitr"]["evidence"])

    def test_unowned_item_fails_the_audit(self):
        m = _score(CHECKLIST[:3] + [{"id": "sec.dns", "area": "security", "item": "dns", "cadence_h": 24}])
        self.assertEqual(_by_id(m)["sec.dns"]["status"], "unowned")
        self.assertTrue(cm.failed(m))
        self.assertTrue(cm.hq_line(None, m).startswith("AUDIT FAIL: "))

    def test_owner_that_is_not_a_member_or_is_disabled_is_unowned(self):
        m = _score([{**CHECKLIST[0], "owner": "ghost"}, {**CHECKLIST[0], "id": "x", "owner": "off"}])
        self.assertEqual([it["status"] for it in m["items"]], ["unowned", "unowned"])

    def test_owner_whose_check_lapsed_past_cadence_is_overdue_and_fails(self):
        self.assertEqual(self.items["jobs.success"]["status"], "overdue")
        self.assertIn("cadence 24h", self.items["jobs.success"]["evidence"])
        self.assertTrue(cm.failed(self.m))

    def test_all_owned_and_fresh_passes(self):
        m = _score(CHECKLIST[:3])
        self.assertFalse(cm.failed(m))
        line = cm.hq_line(m, m)
        self.assertFalse(line.startswith("AUDIT FAIL"), line)
        self.assertIn("all 3 items owned and fresh", line)

    def test_absent_system_is_na_not_failing(self):
        self.assertEqual(self.items["pay.webhooks"]["status"], "n/a")

    def test_lane_that_died_in_a_migration_is_named(self):
        self.assertEqual(self.m["retired_gaps"], [{"lane": "devops", "id": "jobs.success", "source": ""}])

    def test_inventory_system_of_an_uncovered_kind_is_unwatched(self):
        sysmap = {s["id"]: s for s in self.m["systems"]}
        self.assertEqual(sysmap["queue:redis"]["status"], "unwatched")
        self.assertEqual(sysmap["db:postgres"]["status"], "watched")
        self.assertEqual(sysmap["cron:ingest"]["status"], "unwatched")


SOURCES = [
    {"id": "irs-bmf", "name": "BMF", "feeds": "orgs", "owner": "nerd", "fresh_h": 840, "measure": [{"cron": "bmf_refresh"}], "creds": [], "need": "rerun"},
    {"id": "ga4", "name": "GA4", "feeds": "traffic", "owner": "nerd", "fresh_h": 24, "measure": [{"signal": "ga4"}], "creds": ["GA4_SA_KEY"], "need": "fix"},
    {"id": "cf", "name": "CF", "feeds": "edge", "owner": "nerd", "fresh_h": 24, "measure": [{"signal": "cf"}], "creds": ["CF_API_TOKEN"], "need": "port"},
    {"id": "news", "name": "News", "feeds": "news", "owner": "nerd", "fresh_h": 48, "measure": [{"event": "org_news"}], "creds": [], "need": "schedule"},
    {"id": "fac", "name": "FAC", "feeds": "audits", "owner": "nerd", "fresh_h": 720, "measure": [], "creds": ["FAC_API_KEY"], "reif_only": True, "need": "key"},
]


def _capture(bmf_ok=NOW - 5 * 86400, news_ts=NOW - 3600):
    return {"meta": [["now", str(NOW)]], "crontab": [["bmf_refresh"]],
            "cron_exits": [["bmf_refresh", str(bmf_ok), "0", "0", str(bmf_ok)]],
            "envkeys": [["GA4_SA_KEY"], ["CF_API_TOKEN"]],
            "signals": [["ga4", "200", "ok "], ["cf", "503", "cf_api_error Zone Analytics API is sunset"]],
            "inflow_ts": [["org_news", str(news_ts)]]}


class Inflows(unittest.TestCase):
    def _rows(self, **kw):
        return {r["id"]: r for r in inflows.score(SOURCES, _capture(**kw), now=NOW)}

    def test_measured_statuses(self):
        r = self._rows()
        self.assertEqual({k: v["status"] for k, v in r.items()},
                         {"irs-bmf": "OK", "ga4": "OK", "cf": "DOWN", "news": "OK", "fac": "NO_CREDENTIAL"})
        self.assertEqual(r["ga4"]["last_ok"], NOW)
        self.assertIn("sunset", r["cf"]["note"])

    def test_stale_inflow_fails_the_audit(self):
        ok_map = _score(CHECKLIST[:3])
        fresh = [r for r in inflows.score(SOURCES[:2], _capture(), now=NOW)]
        self.assertFalse(cm.failed(ok_map, fresh))
        stale = inflows.score(SOURCES[:2], _capture(bmf_ok=NOW - 40 * 86400), now=NOW)
        self.assertEqual(stale[0]["status"], "STALE")
        self.assertTrue(cm.failed(ok_map, stale))
        line = cm.hq_line(ok_map, ok_map, stale)
        self.assertTrue(line.startswith("AUDIT FAIL: inflows 1/2 OK | STALE: irs-bmf 40d ago || "), line)

    def test_source_with_no_rows_ever_is_down(self):
        r = {x["id"]: x for x in inflows.score(SOURCES, {**_capture(), "inflow_ts": [["org_news", ""]]}, now=NOW)}
        self.assertEqual(r["news"]["status"], "DOWN")

    def test_capture_without_the_section_is_never_checked(self):
        r = {x["id"]: x for x in inflows.score(SOURCES[3:4], {"meta": []}, now=NOW)}
        self.assertEqual(r["news"]["status"], "NEVER_CHECKED")

    def test_issues_one_per_down_or_no_credential_and_one_needs_reif_list(self):
        import issue_cluster
        filed = []
        orig = issue_cluster.file_issue, issue_cluster.list_open
        issue_cluster.list_open = lambda repo: []
        issue_cluster.file_issue = lambda title, body, labels, **kw: filed.append((title, body)) or {"action": "created"}
        try:
            inflows.file_issues(inflows.score(SOURCES, _capture(), now=NOW))
        finally:
            issue_cluster.file_issue, issue_cluster.list_open = orig
        self.assertEqual([t for t, _ in filed], ["data inflow cf is DOWN", "needs Reif: credentials for data inflows"])
        self.assertIn("**Needed:** port", filed[0][1])
        self.assertIn("FAC", filed[1][1])


class RealRepoFiles(unittest.TestCase):
    def test_every_checklist_item_has_exactly_one_existing_member_as_owner(self):
        members = {m["name"] for m in cm.load_members(cm.ROOT / "members")}
        items = json.loads(cm.CHECKLIST.read_text())["items"]
        self.assertEqual(len({it["id"] for it in items}), len(items), "duplicate checklist id")
        for it in items:
            self.assertIsInstance(it.get("owner"), str, f"{it['id']} needs exactly one owner")
            self.assertIn(it["owner"], members, f"{it['id']} owner {it['owner']!r} is not a member")
            self.assertTrue(it.get("check"), f"{it['id']} names no check")
            self.assertGreater(it.get("cadence_h", 0), 0, f"{it['id']} has no cadence_h")
        m = cm.score(items, cm.load_members(cm.ROOT / "members"), [], None, None)
        self.assertFalse(cm.failed(m), [it["id"] for it in m["items"] if it["status"] in cm.FAILING])

    def test_retired_lane_ids_exist_in_the_checklist(self):
        ids = {it["id"] for it in json.loads(cm.CHECKLIST.read_text())["items"]}
        for r in json.loads(cm.RETIRED.read_text())["lanes"]:
            for w in r["watched"]:
                self.assertIn(w, ids, f"retired lane {r['lane']} names unknown id {w!r}")

    def test_every_inflow_is_owned_measured_or_credentialed_and_says_what_is_needed(self):
        members = {m["name"] for m in cm.load_members(cm.ROOT / "members")}
        srcs = inflows.load()
        self.assertEqual(len({s["id"] for s in srcs}), len(srcs))
        for s in srcs:
            self.assertIn(s["owner"], members, s["id"])
            self.assertTrue(s["measure"] or s["creds"], f"{s['id']} is neither measured nor credential-checked")
            self.assertTrue(s["need"] and s["feeds"] and s["fresh_h"] > 0, s["id"])


if __name__ == "__main__":
    unittest.main()
