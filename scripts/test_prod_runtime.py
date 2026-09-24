"""prod_runtime.py: the parser and checks on a real probe capture (atlas-serve, 2026-09-24).

The fixture below is trimmed from the first live run. Every number in it is what a human found by
hand that day and no member was reading: no statement_timeout, the app on the admin role, a
2.5h statement, officers ILIKE at 295ms over 1.2M calls, 107 of 113 connections idle, and a
monthly ingest that has not succeeded in the retained logs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_runtime as pr  # noqa: E402

NOW = 1790277366
FIXTURE = """@@meta
host\t990-Lookup
now\t1790277366
@@pg_settings
max_connections\t200
db_statement_timeout\t0
idle_in_tx_timeout\t1d
@@pg_role_settings
doadmin\t1\t
@@pg_conn
doadmin\t10.116.0.2\tidle\t106
doadmin\t10.116.0.2\tactive\t5
postgres\tlocal\tidle\t1
@@pg_statements_total
1246515\t367795876\t295.1\t14967\t45240266\tSELECT ein, name, title, compensation, tax_year, address, zip FROM officers WHERE name ILIKE $1 LIMIT $2
6301769\t368890866\t58.5\t7204\t216591495\tSELECT ein, emb <=> $1::vector AS distance FROM org_vec_query ORDER BY distance LIMIT $2
@@pg_statements_max
18\t93041685\t5168982.5\t9234914\t4181754\tSELECT g.recipient_ein, MIN(o.name) FROM foundation_grants g JOIN organizations o ON o.ein = g.recipient_ein
1\t138802\t138802.4\t138802\t7\texplain (analyze, buffers) SELECT ein FROM officers WHERE name ILIKE $3
@@pg_long
@@pg_locks
0
@@pg_seqscan
organizations\t22461\t16313203095\t541974610\t2073909
filings\t17\t25675649\t31988102\t3680566
@@pg_writes
org_names_pg\t243\t14534177\t0
@@box
load1\t5.74
load5\t6.06
nproc\t8
mem_total_kb\t16375620
mem_avail_kb\t13484160
disk_used_pct\t20
@@journal
sshd\t3
@@restarts
philanthropy\t0
@@crontab
app_error_canary
determination_letters
ingest_irs_xml
search_zero_result_canary
@@cron_exits
app_error_canary\t1790277307\t0\t26\t1790277307
determination_letters\t1788238804\t1\t0\t0
ingest_irs_xml\t1789878649\t0\t0\t1789878649
search_zero_result_canary\t1790277309\t1\t66\t1790276412
retired_job\t1790277309\t1\t66\t0
"""


class Parser(unittest.TestCase):
    def test_pg_stat_statements_rows_parse_into_numbers_and_query(self):
        p = pr.parse(FIXTURE)
        s = pr.statements(p["pg_statements_total"])
        self.assertEqual(len(s), 2)
        self.assertEqual(s[0]["calls"], 1246515)
        self.assertEqual(s[0]["mean_ms"], 295.1)
        self.assertEqual(s[0]["max_ms"], 14967)
        self.assertTrue(s[0]["query"].startswith("SELECT ein, name, title"))
        self.assertIn("ILIKE $1", s[0]["query"])
        m = pr.statements(p["pg_statements_max"])
        self.assertEqual(m[0]["max_ms"], 9234914)

    def test_empty_section_is_present_but_empty(self):
        p = pr.parse(FIXTURE)
        self.assertEqual(p["pg_long"], [])
        self.assertEqual(p["pg_locks"], [["0"]])


class Checks(unittest.TestCase):
    def setUp(self):
        self.f = {x["check"]: x for x in pr.evaluate(pr.parse(FIXTURE), now=NOW,
                                                     registry={"app_error_canary", "ingest_irs_xml"})}

    def test_the_hand_found_problems_are_all_flagged(self):
        self.assertEqual(self.f["db-statement-timeout"]["severity"], "breach")
        self.assertIn("doadmin", self.f["db-statement-timeout"]["evidence"][0])
        self.assertEqual(self.f["db-runaway-queries"]["severity"], "breach")
        self.assertIn("9234.9s", self.f["db-runaway-queries"]["evidence"][0])
        self.assertIn("officers WHERE name ILIKE", self.f["db-slow-queries"]["evidence"][0])
        self.assertIn("doadmin", self.f["db-least-privilege"]["title"])
        self.assertIn("107 of 112", self.f["db-pool-oversized"]["title"])
        self.assertIn("organizations", self.f["db-index-miss"]["evidence"][0])
        self.assertIn("org_names_pg", self.f["db-heavy-writes"]["evidence"][0])
        self.assertEqual(self.f["ingest-freshness"]["severity"], "breach")
        self.assertIn("determination_letters", self.f["ingest-freshness"]["evidence"][0])

    def test_high_volume_fast_query_is_not_slow(self):
        # 6.3M calls at 58ms mean: expensive in total, but under the mean bar -- not a slow query.
        self.assertNotIn("org_vec_query", " ".join(self.f["db-slow-queries"]["evidence"]))

    def test_cron_failures_only_count_installed_jobs(self):
        ev = " ".join(self.f["cron-failures"]["evidence"])
        self.assertIn("search_zero_result_canary", ev)
        self.assertNotIn("retired_job", ev)  # a log left behind by a removed cron is not a failure

    def test_cron_drift_against_the_repo_registry(self):
        ev = self.f["cron-drift"]["evidence"]
        self.assertIn("determination_letters", ev[0])

    def test_healthy_box_is_not_saturated(self):
        self.assertNotIn("box-saturation", self.f)
        self.assertNotIn("db-locks", self.f)

    def test_probe_error_is_a_breach_not_silence(self):
        f = pr.evaluate(pr.parse("@@meta\nerror\tno probe access\n"), now=NOW)
        self.assertEqual([x["check"] for x in f], ["probe-error"])
        self.assertEqual(f[0]["severity"], "breach")


class HqPush(unittest.TestCase):
    def test_breach_starts_once_and_resolves_once(self):
        d = Path(tempfile.mkdtemp())
        state, alerts = d / "s.json", d / "prod-alerts.jsonl"
        breach = [{"check": "db-statement-timeout", "severity": "breach", "title": "t", "evidence": ["e"]}]
        self.assertEqual(len(pr.push_transitions(breach, "h", state, alerts, now=NOW)), 1)
        self.assertEqual(pr.push_transitions(breach, "h", state, alerts, now=NOW + 60), [])
        out = pr.push_transitions([], "h", state, alerts, now=NOW + 120)
        self.assertEqual([r["transition"] for r in out], ["resolve"])
        rows = [json.loads(x) for x in alerts.read_text().splitlines()]
        self.assertEqual([r["transition"] for r in rows], ["start", "resolve"])
        self.assertEqual(rows[0]["signature"], "prod-runtime:db-statement-timeout")

    def test_issue_title_is_stable_across_ticks(self):
        a = pr.issue_title({"check": "cron-failures", "severity": "finding"})
        self.assertNotRegex(a, r"\d")


if __name__ == "__main__":
    unittest.main()
