"""retro.py and audit_forward.py: the daily retro stays short, and each hole reaches dumbledore once."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import audit_forward  # noqa: E402
import fleet_metrics  # noqa: E402
import retro  # noqa: E402
from fleet_tz import CENTRAL  # noqa: E402

INTENT = """# INTENT.md
- 2026-10-16 "I thought we didnt do hospitals - its muddying the experience" -> hospitals out
- 2026-10-16 "superadmin still redirects here" -> superadmin is its own host
- 2026-10-16 "Ok but did the experience improve for the user" -> a user number moved
- 2026-10-16 "one" -> x
- 2026-10-16 "two" -> x
- 2026-10-15 "an older one" -> x
"""
BOARD = """| Found by | Holes |
|---|---|
| Auditor (lane:audit) | 3 |
| Viken (open rows in viken-status.md) | 12 |
| By hand on Jobs (not lane:audit) | 33 |
"""
KPI = {"claims": {"verified_orgs": 3, "daily_30d": [
    {"d": "2026-10-15", "submitted": 30, "approved": 13},
    {"d": "2026-10-16", "submitted": 14, "approved": 28}]},
    "revenue": {"new_subscriptions_daily_30d": [{"d": "2026-10-16", "n": 1}]},
    "signups": {"daily_30d": [{"d": "2026-10-15", "n": 127}, {"d": "2026-10-16", "n": 100}]},
    "jobs": {"applications": {"last_7d": 1}}}


class Retro(unittest.TestCase):
    def test_only_yesterdays_words_and_the_lane_that_missed_them(self):
        said = retro.directions("2026-10-16", INTENT, Path("/nonexistent"))
        self.assertEqual(len(said), 5)
        self.assertNotIn("an older one", said)
        self.assertEqual(retro.lane_for(said[0]), "auditor")
        self.assertEqual(retro.lane_for(said[1]), "sentry")
        self.assertEqual(retro.lane_for(said[2]), "persona walk")

    def test_inbox_copies_when_intent_has_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "20261016-231649.md").write_text("Hospitals are back in Jobs. Please fix.\n")
            Path(d, "20261017-000845.md").write_text("Not yesterday.\n")
            self.assertEqual(retro.directions("2026-10-16", "", Path(d)), ["Hospitals are back in Jobs"])

    def test_retro_is_ten_lines_max_with_every_section(self):
        said = retro.directions("2026-10-16", INTENT, Path("/nonexistent"))
        text = retro.render("2026-10-16", said * 3, "none registered",
                            retro.scoreboard(KPI, "2026-10-16", "2026-10-15"),
                            retro.budget(None), retro.audit(BOARD))
        lines = text.splitlines()
        self.assertLessEqual(len(lines), retro.MAX_LINES)
        for label in ("Charter change:", "Product:", "Budget:", "Audit:"):
            self.assertTrue(any(x.startswith(label) for x in lines), label)
        self.assertIn("claims filed 14 (was 30)", text)
        self.assertIn("Scout+ sales 1 (was ?)", text)
        self.assertIn("auditor 3, Viken 12", text)

    def test_charter_change_is_yesterdays_newest_row(self):
        noon = dt.datetime(2026, 10, 16, 12, tzinfo=CENTRAL).timestamp()
        rows = [{"id": 1, "ts": noon, "member": "dumbledore", "change": "fleet-kit#1", "note": "a"},
                {"id": 2, "ts": noon + 60, "member": "g", "change": "fleet-kit#2", "note": "b"},
                {"id": 3, "ts": noon + 86400, "member": "g", "change": "fleet-kit#3", "note": "c"}]
        self.assertTrue(retro.charter_change(rows, "2026-10-16").startswith("#2 g fleet-kit#2"))
        self.assertEqual(retro.charter_change(rows, "2026-10-01"), "none registered")

    def test_missing_inputs_say_no_reading_never_zero(self):
        self.assertIn("no reading", retro.scoreboard(None, "2026-10-16", "2026-10-15"))
        self.assertIn("no reading", retro.budget(None))
        self.assertEqual(retro.audit(""), "no scoreboard yet")
        meter = {"rows": [{"name": "tgp", "error": "x"},
                          {"name": "reif", "week_used": 49, "elapsed": 52}]}
        self.assertEqual(retro.budget(meter), "tgp no reading; reif 49% used, 52% of week gone")

    def test_audit_lead_metric_reads_the_scoreboard(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "audit-scoreboard.md")
            with mock.patch.object(retro, "SCOREBOARD", p):
                self.assertIsNone(fleet_metrics.compute("audit_lead", []))
                p.write_text(BOARD)
                self.assertEqual(fleet_metrics.compute("audit_lead", []), -9.0)
        self.assertEqual(fleet_metrics.direction("audit_lead"), "higher")

    def test_cron_runs_the_retro_at_seven_central(self):
        sh = (ROOT / "entrypoint.sh").read_text()
        self.assertIn('emit_env_required TZ "America/Chicago"', sh)
        self.assertIn('echo "0 7 * * * root', sh[sh.index("retro.py") - 900:sh.index("retro.log")])


ISSUES = [{"number": 7, "title": "CSV link is the PDF", "body": "x\nAudit-pattern: csv_link_is_pdf\n"},
          {"number": 9, "title": "again", "body": "Audit-pattern: csv_link_is_pdf"},
          {"number": 8, "title": "no pattern line", "body": "hand-filed"}]


class AuditForward(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"FLEET_LOG_DIR": self.d})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_one_message_per_pattern_and_never_twice(self):
        calls = []
        with mock.patch.object(audit_forward, "open_audit_issues", return_value=ISSUES), \
                mock.patch.object(subprocess, "run", side_effect=lambda a, **k: calls.append(a)):
            audit_forward.main(["send"])
            audit_forward.main(["send"])
        self.assertEqual(len(calls), 1)
        self.assertIn("audit:csv_link_is_pdf", calls[0])
        self.assertIn("dumbledore", calls[0])
        self.assertIn("#7", calls[0][-1])

    def test_pending_fires_only_on_a_new_pattern(self):
        with mock.patch.object(audit_forward, "open_audit_issues", return_value=ISSUES), \
                mock.patch("builtins.print") as out:
            audit_forward.main(["pending"])
            Path(self.d, "audit_forwarded.json").write_text(json.dumps(["csv_link_is_pdf"]))
            audit_forward.main(["pending"])
        self.assertTrue(out.call_args_list[0].args[0].startswith("FIRE 1"))
        self.assertTrue(out.call_args_list[1].args[0].startswith("green"))

    def test_jefe_pregate_asks_audit_forward_when_the_inbox_is_empty(self):
        sh = (ROOT / "members" / "jefe" / "pregate.sh").read_text()
        self.assertIn("audit_forward.py\" pending", sh)
        self.assertIn("audit_forward.py send", (ROOT / "members" / "jefe" / "jefe.md").read_text())
        self.assertIn("A hole a lane missed",
                      (ROOT / "members" / "dumbledore" / "dumbledore.md").read_text())


if __name__ == "__main__":
    unittest.main()
