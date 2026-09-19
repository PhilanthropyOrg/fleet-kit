"""fk#1129 slice 3: the morning brief's "Came in yesterday" section needs to say not just how
many things landed in the intake store, but what happened to each one -- a board item created,
a comment added, an ask answered, a steering issue filed, or dropped. Before this, `apply()`
called `mark_done(id)` with no result, so nothing downstream of DONE could ever answer "what
happened" -- only "is it done".

Run: python3 scripts/test_inbox_came_in.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import inbox  # noqa: E402


def _write_inbox_row(path: Path, **fields) -> None:
    row = {"id": fields.pop("id"), "received_at": fields.pop("received_at", time.time()),
           "from": "someone@example.com", "subject": "s", "text": "t", "trusted": True}
    row.update(fields)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


class CameInTests(unittest.TestCase):
    def _paths(self, tmp: str) -> tuple[Path, Path]:
        return Path(tmp) / "inbox.jsonl", Path(tmp) / "inbox.results.jsonl"

    def test_seeded_rows_roll_up_counts_and_the_alert_summary_line(self):
        """3 alerts on the same check (1 create + 2 comments), 1 question, 1 forward, 1
        github -- came_in() returns counts {alert:3, question:1, forward:1, github:1} and the
        alert summary reads '1 board item, 2 comments'."""
        with tempfile.TemporaryDirectory() as tmp:
            inbox_path, results_path = self._paths(tmp)
            with unittest.mock.patch.object(inbox, "INBOX", inbox_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path):
                now = time.time()
                _write_inbox_row(inbox_path, id="a1", kind="alert", received_at=now)
                _write_inbox_row(inbox_path, id="a2", kind="alert", received_at=now)
                _write_inbox_row(inbox_path, id="a3", kind="alert", received_at=now)
                _write_inbox_row(inbox_path, id="q1", kind="question", received_at=now)
                _write_inbox_row(inbox_path, id="f1", kind="forward", received_at=now)
                _write_inbox_row(inbox_path, id="g1", kind="github", received_at=now)

                inbox.mark_done("a1", result="board item #412 created")
                inbox.mark_done("a2", result="comment on #412")
                inbox.mark_done("a3", result="comment on #412")
                inbox.mark_done("q1", result="dropped: already handled by the product")
                inbox.mark_done("f1", result="steering issue #55")
                inbox.mark_done("g1", result="dropped: no action needed")

                out = inbox.came_in(now - 3600)

        self.assertEqual(out["counts"], {"alert": 3, "question": 1, "forward": 1, "github": 1})
        self.assertEqual(out["summary"]["alert"], "1 board item, 2 comments")
        self.assertEqual(out["dropped_total"], 2)

    def test_empty_store_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox_path, results_path = self._paths(tmp)
            with unittest.mock.patch.object(inbox, "INBOX", inbox_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path):
                out = inbox.came_in(time.time() - 86400)
        self.assertEqual(out["counts"], {})
        self.assertEqual(out["summary"], {})
        self.assertEqual(out["dropped_total"], 0)

    def test_rows_outside_the_window_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox_path, results_path = self._paths(tmp)
            with unittest.mock.patch.object(inbox, "INBOX", inbox_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path):
                old = time.time() - 2 * 86400
                _write_inbox_row(inbox_path, id="old1", kind="alert", received_at=old)
                out = inbox.came_in(time.time() - 86400)
        self.assertEqual(out["counts"], {})

    def test_row_with_no_result_yet_reads_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox_path, results_path = self._paths(tmp)
            with unittest.mock.patch.object(inbox, "INBOX", inbox_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path):
                now = time.time()
                _write_inbox_row(inbox_path, id="p1", kind="steering", received_at=now)
                out = inbox.came_in(now - 3600)
        self.assertEqual(out["lines"]["steering"], ["pending"])
        self.assertEqual(out["summary"]["steering"], "1 pending")


class MarkDoneRecordsResultTests(unittest.TestCase):
    """Mutation gate: mark_done(result=...) must actually write the result, and a bare
    mark_done(id) (no result) must not write anything to RESULTS -- so a row that genuinely
    did nothing stays "pending", not a fabricated empty result line."""

    def test_result_is_persisted_and_readable_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            done_path, results_path = Path(tmp) / "inbox.done", Path(tmp) / "inbox.results.jsonl"
            with unittest.mock.patch.object(inbox, "DONE", done_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path), \
                 unittest.mock.patch.object(inbox, "LOG_DIR", Path(tmp)):
                inbox.mark_done("x1", result="answered ask #9")
            rows = [json.loads(l) for l in results_path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "x1")
        self.assertEqual(rows[0]["result"], "answered ask #9")

    def test_no_result_writes_nothing_to_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            done_path, results_path = Path(tmp) / "inbox.done", Path(tmp) / "inbox.results.jsonl"
            with unittest.mock.patch.object(inbox, "DONE", done_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path), \
                 unittest.mock.patch.object(inbox, "LOG_DIR", Path(tmp)):
                inbox.mark_done("x2")
            self.assertFalse(results_path.exists())


class ApplyRecordsAlertResultTests(unittest.TestCase):
    """First firing of a check creates a board item and records 'board item #N created'; the
    next firing of the same check finds it open, comments, and records 'comment on #N'."""

    def test_first_alert_creates_second_alert_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            done_path, results_path = Path(tmp) / "inbox.done", Path(tmp) / "inbox.results.jsonl"
            calls = []

            def fake_run(cmd):
                calls.append(cmd)
                r = unittest.mock.Mock()
                if cmd[:3] == ["gh", "issue", "list"]:
                    if len(calls) == 1:
                        r.returncode, r.stdout = 0, "[]"
                    else:
                        r.returncode = 0
                        r.stdout = json.dumps([{"number": 412, "title": "prod alert [app_error]",
                                                "url": "https://github.com/x/y/issues/412"}])
                elif cmd[:3] == ["gh", "issue", "create"]:
                    r.returncode, r.stdout, r.stderr = 0, "https://github.com/x/y/issues/412\n", ""
                elif cmd[:3] == ["gh", "issue", "comment"]:
                    r.returncode, r.stdout, r.stderr = 0, "", ""
                else:
                    r.returncode, r.stdout, r.stderr = 0, "", ""
                return r

            with unittest.mock.patch.object(inbox, "DONE", done_path), \
                 unittest.mock.patch.object(inbox, "RESULTS", results_path), \
                 unittest.mock.patch.object(inbox, "LOG_DIR", Path(tmp)), \
                 unittest.mock.patch.dict("os.environ", {"FLEET_REPO_URL": "https://github.com/x/y"}):
                row1 = {"id": "a1", "kind": "alert", "check": "app_error", "subject": "990 Scout prod alert [app_error]: boom", "text": "boom"}
                inbox.apply(row1, run=fake_run)
                row2 = {"id": "a2", "kind": "alert", "check": "app_error", "subject": "990 Scout prod alert [app_error]: boom again", "text": "boom again"}
                inbox.apply(row2, run=fake_run)
            results = {json.loads(l)["id"]: json.loads(l)["result"] for l in results_path.read_text().splitlines()}
        self.assertEqual(results["a1"], "board item #412 created")
        self.assertEqual(results["a2"], "comment on #412")


class DedupeLookupTests(unittest.TestCase):
    """fk#1154: the open-twin lookup is the REST list, never the search flag. Search is a
    30/min bucket shared by every member and its index lags a fresh issue, which is how
    [app_error] filed a new issue every hour for three hours with the last one still open."""

    def _run_both_filers(self, list_rc, list_stdout):
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            r = unittest.mock.Mock()
            r.stderr = ""
            if cmd[:3] == ["gh", "issue", "list"]:
                r.returncode, r.stdout = list_rc, list_stdout
            elif cmd[:3] == ["gh", "issue", "create"]:
                r.returncode, r.stdout = 0, "https://github.com/x/y/issues/900\n"
            else:
                r.returncode, r.stdout = 0, ""
            return r

        logged = []
        with unittest.mock.patch.dict("os.environ", {"FLEET_REPO_URL": "https://github.com/x/y"}), \
             unittest.mock.patch.object(inbox, "log", logged.append):
            alert = inbox.file_or_comment_alert({"check": "app_error", "text": "boom"}, run=fake_run)
            mail = inbox.file_backlog("prod alert [app_error]", "boom", "box@x", run=fake_run)
        return calls, logged, alert, mail

    def test_lookup_is_the_rest_list_not_search(self):
        twin = json.dumps([{"number": 7, "title": "prod alert [app_error]", "url": "u7"}])
        calls, _, alert, mail = self._run_both_filers(0, twin)
        lists = [c for c in calls if c[:3] == ["gh", "issue", "list"]]
        self.assertEqual(len(lists), 2, "both filers must look before filing")
        for c in lists:
            self.assertNotIn("--search", c, c)
        self.assertEqual(alert, ("u7", False))
        self.assertEqual(mail, "u7")
        self.assertFalse([c for c in calls if c[:3] == ["gh", "issue", "create"]], "twin filed")

    def test_a_failed_lookup_is_logged_and_still_files(self):
        calls, logged, alert, _ = self._run_both_filers(1, "")
        self.assertEqual(len([c for c in calls if c[:3] == ["gh", "issue", "create"]]), 2)
        self.assertTrue(alert[1], "first firing with no readable board must still create")
        self.assertTrue([m for m in logged if "dedupe lookup failed" in m], logged)


if __name__ == "__main__":
    unittest.main()
