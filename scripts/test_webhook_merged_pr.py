"""Tests for webhook_receiver.py's merged-PR line (Reif HQ notify task).

Reif: Reif HQ (dino, tmux 'reif') must hear about every PR merged to main. This is the
receiver-side half -- one JSON line per merge appended to logs/merged-prs.jsonl, host-side
watcher (systemd .path unit) does the rest.

Run: python3 scripts/test_webhook_merged_pr.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webhook_receiver as wr  # noqa: E402


def _pr_payload(**overrides):
    payload = {
        "action": "closed",
        "repository": {"default_branch": "main"},
        "pull_request": {
            "number": 42,
            "title": "Fix the thing",
            "html_url": "https://github.com/acme/repo/pull/42",
            "merged": True,
            "merged_at": "2026-09-24T12:00:00Z",
            "user": {"login": "reif"},
            "body": "Closes #10 and fixes #10, also resolves #11.",
            "base": {"ref": "main"},
        },
    }
    payload.update(overrides)
    return payload


class ClosesRefsTests(unittest.TestCase):
    def test_dedupes_and_matches_close_fix_resolve_any_tense(self):
        body = "Closes #10 and fixes #10, also resolves #11. Unrelated #999 in prose? no ref word."
        self.assertEqual(wr.closes_refs(body), [10, 11])

    def test_no_refs_returns_empty(self):
        self.assertEqual(wr.closes_refs("just a plain description"), [])


class MergedPrRecordTests(unittest.TestCase):
    def test_record_has_required_fields(self):
        record = wr.merged_pr_record(_pr_payload())
        self.assertEqual(record["number"], 42)
        self.assertEqual(record["title"], "Fix the thing")
        self.assertEqual(record["url"], "https://github.com/acme/repo/pull/42")
        self.assertEqual(record["merged_at"], "2026-09-24T12:00:00Z")
        self.assertEqual(record["author"], "reif")
        self.assertEqual(record["closes"], [10, 11])


class RecordMergedPrWritesLineTests(unittest.TestCase):
    def test_appends_one_json_line_per_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            orig_log_dir, orig_file = wr.LOG_DIR, wr.MERGED_PRS_FILE
            wr.LOG_DIR = log_dir
            wr.MERGED_PRS_FILE = log_dir / "merged-prs.jsonl"
            try:
                wr.record_merged_pr(_pr_payload())
                wr.record_merged_pr(_pr_payload(pull_request={**_pr_payload()["pull_request"], "number": 43}))
                lines = wr.MERGED_PRS_FILE.read_text().splitlines()
                self.assertEqual(len(lines), 2)
                rows = [json.loads(l) for l in lines]
                self.assertEqual([r["number"] for r in rows], [42, 43])
            finally:
                wr.LOG_DIR, wr.MERGED_PRS_FILE = orig_log_dir, orig_file

    def test_merge_into_non_default_branch_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            orig_log_dir, orig_file = wr.LOG_DIR, wr.MERGED_PRS_FILE
            wr.LOG_DIR = log_dir
            wr.MERGED_PRS_FILE = log_dir / "merged-prs.jsonl"
            try:
                payload = _pr_payload()
                payload["pull_request"]["base"] = {"ref": "release/1.0"}
                wr.record_merged_pr(payload)
                self.assertFalse(wr.MERGED_PRS_FILE.exists())
            finally:
                wr.LOG_DIR, wr.MERGED_PRS_FILE = orig_log_dir, orig_file


if __name__ == "__main__":
    unittest.main()
