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
        for st in ("ok", "killed", "timed_out", "budget_declined"):
            self.assertTrue(run_mail.wants_mail({**REC, "status": st}), st)
        # reported_nothing (2026-09-12, Reif: "I dont want to get this mail anymore") is real
        # work-loss and still counted in fleet_metrics.py -- it just doesn't page a human.
        for st in ("started", "heartbeat", "quiet", "paced", "reported_nothing"):
            self.assertFalse(run_mail.wants_mail({**REC, "status": st}), st)

    def test_off_unless_flag_is_exactly_1(self):
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": ""}):
            self.assertEqual(run_mail.maybe_mail(REC), "off")
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": "true"}):
            self.assertEqual(run_mail.maybe_mail(REC), "off")


class Compose(unittest.TestCase):
    """Reif, 2026-09-12: "reports in plain english please" -- the WHOLE mail reads plainly,
    not a plain paragraph stapled on top of the member's raw jargon."""

    def test_plain_words_are_the_body_and_jargon_sits_below_the_line(self):
        md = run_mail.compose(REC, "The helper opened a change for you to look at.")
        self.assertTrue(md.startswith("# minion finished"), md[:60])
        # The plain rewrite comes before the divider; the raw report comes after it.
        self.assertLess(md.index("The helper opened a change"), md.index("---"))
        self.assertGreater(md.index("BOTTOM LINE"), md.index("---"))
        # Raw status words never reach the reader as the headline.
        self.assertNotIn("# minion · ok", md)

    def test_every_status_gets_plain_words_not_a_code_word(self):
        for status, want in (("killed", "was interrupted"),
                             ("budget_declined", "stopped to stay inside its budget"),
                             ("reported_nothing", "did not say what it did")):
            md = run_mail.compose({**REC, "status": status}, "x")
            self.assertIn(want, md.splitlines()[0])

    def test_orphan_bold_marker_from_a_member_is_not_rendered(self):
        """Live: a minion report began with `**\\n\\n`, printing a bare ** in Reif's inbox."""
        md = run_mail.compose({**REC, "report": "**\n\nBOTTOM LINE: shipped."}, "x")
        self.assertNotIn("\n**\n", md)
        self.assertIn("BOTTOM LINE: shipped.", md)

    def test_missing_rewrite_says_so_and_still_carries_the_report(self):
        md = run_mail.compose(REC, "")
        self.assertIn("could not be rewritten in plain words", md)
        self.assertIn("BOTTOM LINE: shipped.", md)


class RepoLinks(unittest.TestCase):
    """The first real run mail linked to https://github.com//repo/issues/4996 -- FLEET_REPO is
    the checkout PATH inside the container, never an owner/name slug."""

    def test_a_container_path_never_becomes_a_link(self):
        with unittest.mock.patch.dict(os.environ, {"FLEET_REPO": "/repo", "FLEET_REPO_URL": ""}, clear=False):
            self.assertEqual(run_mail.repo_slug(), "")
            md = run_mail.compose(REC, "x")
        self.assertNotIn("github.com//repo", md)
        self.assertNotIn("github.com/", md)

    def test_slug_comes_from_the_git_remote(self):
        with unittest.mock.patch.dict(os.environ, {
                "FLEET_REPO": "/repo",
                "FLEET_REPO_URL": "https://github.com/The-Good-Project-Team/philanthropy.git"}, clear=False):
            self.assertEqual(run_mail.repo_slug(), "The-Good-Project-Team/philanthropy")
            md = run_mail.compose(REC, "x")
        self.assertIn("https://github.com/The-Good-Project-Team/philanthropy/pull/12", md)
        self.assertIn("https://github.com/The-Good-Project-Team/philanthropy/issues/7", md)

    def test_ssh_remote_and_no_remote_both_handled(self):
        with unittest.mock.patch.dict(os.environ, {"FLEET_REPO_URL": "git@github.com:owner/name.git"}, clear=False):
            self.assertEqual(run_mail.repo_slug(), "owner/name")
        with unittest.mock.patch.dict(os.environ, {"FLEET_REPO": "", "FLEET_REPO_URL": ""}, clear=False):
            self.assertEqual(run_mail.repo_slug(), "")


class Delivery(unittest.TestCase):
    def test_delivers_as_kind_run_with_no_daily_dedupe(self):
        import messenger_brief as mb
        sent = []
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": "1", "FLEET_RUN_MAIL_REENABLE": "1"}), \
             unittest.mock.patch.object(run_mail, "plain_words", return_value="Plain."), \
             unittest.mock.patch.object(mb, "deliver", side_effect=lambda k, md, want_pdf, force: (sent.append((k, md, force)) or (0, "sent"))):
            self.assertEqual(run_mail.maybe_mail(REC), "sent")
            self.assertEqual(run_mail.maybe_mail(REC), "sent")
        self.assertEqual([s[0] for s in sent], ["run", "run"])
        self.assertTrue(all(s[2] for s in sent))
        self.assertIn("run", mb.PER_EVENT_KINDS)

    def test_delivery_failure_never_raises(self):
        import messenger_brief as mb
        with unittest.mock.patch.dict(os.environ, {"FLEET_RUN_MAIL": "1", "FLEET_RUN_MAIL_REENABLE": "1"}), \
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
