"""run_mail.py: one plain-English email per finished run (Reif, 2026-09-12: "have those reports
sent to me each time something runs"), and run_report.py is the one place that triggers it."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
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


class PlainWordsFailuresAreDiagnosable(unittest.TestCase):
    """gh#6287/#6288/#6289/#6334/#6343: 0 of 98 sampled finished runs ever carried a `plain`
    field, and every one of plain_words()'s four failure paths collapsed into an
    indistinguishable "" with zero trace. Each path must now log which one fired."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log_file = Path(self._tmpdir.name) / "plain-words.log"
        patcher = unittest.mock.patch.object(run_mail, "PLAIN_LOG_FILE", self.log_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _last_reason(self):
        lines = self.log_file.read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_no_account_pool_is_logged(self):
        with unittest.mock.patch.object(run_mail, "KIT", Path("/does-not-exist")):
            self.assertEqual(run_mail.plain_words({"member": "m", "run_id": "r1"}), "")
        self.assertEqual(self._last_reason()["reason"], "no-account-pool")

    def test_timeout_is_logged(self):
        with unittest.mock.patch.object(subprocess, "run",
                                         side_effect=subprocess.TimeoutExpired(cmd="x", timeout=120)):
            self.assertEqual(run_mail.plain_words({"member": "m", "run_id": "r2"}), "")
        entry = self._last_reason()
        self.assertEqual(entry["reason"], "timeout")
        self.assertEqual(entry["run_id"], "r2")

    def test_subprocess_error_is_logged(self):
        with unittest.mock.patch.object(subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(run_mail.plain_words({"member": "m", "run_id": "r3"}), "")
        self.assertEqual(self._last_reason()["reason"], "subprocess-error")

    def test_non_zero_exit_is_logged_with_rc_and_stderr(self):
        result = subprocess.CompletedProcess(args=["x"], returncode=3, stdout="", stderr="pool exhausted")
        with unittest.mock.patch.object(subprocess, "run", return_value=result):
            self.assertEqual(run_mail.plain_words({"member": "m", "run_id": "r4"}), "")
        entry = self._last_reason()
        self.assertEqual(entry["reason"], "non-zero-exit")
        self.assertIn("rc=3", entry["detail"])
        self.assertIn("pool exhausted", entry["detail"])

    def test_oversized_output_is_logged(self):
        result = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="x" * 2000, stderr="")
        with unittest.mock.patch.object(subprocess, "run", return_value=result):
            self.assertEqual(run_mail.plain_words({"member": "m", "run_id": "r5"}), "")
        self.assertEqual(self._last_reason()["reason"], "empty-or-oversized-output")

    def test_success_writes_no_log_line(self):
        result = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="A plain sentence.", stderr="")
        with unittest.mock.patch.object(subprocess, "run", return_value=result):
            self.assertEqual(run_mail.plain_words({"member": "m", "run_id": "r6"}), "A plain sentence.")
        self.assertFalse(self.log_file.exists())

    def test_plain_words_for_exception_is_logged_even_if_run_mail_call_raises(self):
        with unittest.mock.patch.object(run_mail, "wants_mail", side_effect=RuntimeError("kaboom")), \
             unittest.mock.patch.dict(os.environ, {"FLEET_RUN_PLAIN": "1", "FLEET_LOG_DIR": self._tmpdir.name}):
            rec = {"member": "m", "run_id": "r7", "outcome": "did a thing"}
            self.assertEqual(run_report.plain_words_for(rec), "")
        log_file = Path(self._tmpdir.name) / "plain-words.log"
        entry = json.loads(log_file.read_text().strip().splitlines()[-1])
        self.assertEqual(entry["reason"], "plain-words-for-exception")
        self.assertIn("kaboom", entry["detail"])


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
