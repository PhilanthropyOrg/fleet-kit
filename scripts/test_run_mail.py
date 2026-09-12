"""run_mail.py: one plain-English email per finished run (Reif, 2026-09-12: "have those reports
sent to me each time something runs"), and run_report.py is the one place that triggers it."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_mail  # noqa: E402
import run_report  # noqa: E402

REC = {"member": "minion", "run_id": "minion-1", "status": "ok", "outcome": "Opened PR #12",
       "evidence": "gh pr view 12", "report": "BOTTOM LINE: shipped.", "self_critique": "none",
       "pr": "12", "item_id": "7", "tokens": {"cost_usd": 0.42, "num_turns": 9}}


class WhichRunsMail(unittest.TestCase):
    def test_finished_runs_mail_liveness_rows_do_not(self):
        for st in ("ok", "reported_nothing", "killed", "timed_out", "budget_declined"):
            self.assertTrue(run_mail.wants_mail({**REC, "status": st}), st)
        for st in ("started", "heartbeat", "quiet", "paced"):
            self.assertFalse(run_mail.wants_mail({**REC, "status": st}), st)

    def test_off_unless_flag_is_exactly_1(self):
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": ""}):
            self.assertEqual(run_mail.maybe_mail(REC), "off")
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": "true"}):
            self.assertEqual(run_mail.maybe_mail(REC), "off")


class Compose(unittest.TestCase):
    def test_plain_words_come_first_then_the_members_own_report(self):
        md = run_mail.compose(REC, "The helper opened a change for you to look at.")
        self.assertTrue(md.startswith("# minion · ok · Opened PR #12"))
        self.assertLess(md.index("In plain words"), md.index("BOTTOM LINE"))
        self.assertIn("PR #12", md)
        self.assertIn("item #7", md)
        self.assertIn("gh pr view 12", md)

    def test_missing_rewrite_says_so_and_still_carries_the_report(self):
        md = run_mail.compose(REC, "")
        self.assertIn("rewrite unavailable", md)
        self.assertIn("BOTTOM LINE: shipped.", md)


class Delivery(unittest.TestCase):
    def test_delivers_as_kind_run_with_no_daily_dedupe(self):
        import messenger_brief as mb
        sent = []
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": "1"}), \
             unittest.mock.patch.object(run_mail, "plain_words", return_value="Plain."), \
             unittest.mock.patch.object(mb, "deliver", side_effect=lambda k, md, want_pdf, force: (sent.append((k, md, force)) or (0, "sent"))):
            self.assertEqual(run_mail.maybe_mail(REC), "sent")
            self.assertEqual(run_mail.maybe_mail(REC), "sent")
        self.assertEqual([s[0] for s in sent], ["run", "run"])
        self.assertTrue(all(s[2] for s in sent))
        self.assertIn("run", mb.PER_EVENT_KINDS)

    def test_delivery_failure_never_raises(self):
        import messenger_brief as mb
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": "1"}), \
             unittest.mock.patch.object(run_mail, "plain_words", return_value=""), \
             unittest.mock.patch.object(mb, "deliver", side_effect=RuntimeError("boom")), \
             unittest.mock.patch.object(mb, "log"):
            self.assertEqual(run_mail.maybe_mail(REC), "error")


class RunReportIsTheChokepoint(unittest.TestCase):
    def test_run_report_main_hands_every_record_to_run_mail(self):
        seen = []
        with unittest.mock.patch.object(run_mail, "maybe_mail", side_effect=lambda r: seen.append(r) or "off"), \
             unittest.mock.patch("sys.stdin", io.StringIO("Outcome: did #1\nEvidence: ran it\n")), \
             unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = run_report.main(["--member", "minion", "--run-id", "r1", "--pass-file", "-"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["member"], "minion")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["run_id"], "r1")


if __name__ == "__main__":
    unittest.main()
