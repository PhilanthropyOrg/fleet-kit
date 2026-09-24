"""Tests for notify_reif_hq.py -- Reif HQ merged-PR notification (host-side tmux injector).

Run: python3 scripts/test_notify_reif_hq.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_reif_hq as hq  # noqa: E402


class SanitizeTitleTests(unittest.TestCase):
    def test_strips_control_chars_and_backticks(self):
        self.assertEqual(hq.sanitize_title("evil`rm -rf /`\x07title"), "evil'rm -rf /'title")

    def test_clips_to_120_chars(self):
        long_title = "x" * 200
        out = hq.sanitize_title(long_title)
        self.assertEqual(len(out), 120)
        self.assertTrue(out.endswith("..."))

    def test_collapses_embedded_newlines(self):
        self.assertEqual(hq.sanitize_title("line one\nline two"), "line one line two")


class FormatMessageTests(unittest.TestCase):
    def test_single_pr_message(self):
        msg = hq.format_message([{"number": 5, "title": "Fix bug", "url": "https://x/5"}])
        self.assertIn("PR #5 merged: Fix bug https://x/5", msg)
        self.assertIn("Done-means-live", msg)

    def test_batched_message_has_one_line_per_pr(self):
        msg = hq.format_message([
            {"number": 1, "title": "A", "url": "https://x/1"},
            {"number": 2, "title": "B", "url": "https://x/2"},
        ])
        self.assertIn("PR #1 merged: A https://x/1", msg)
        self.assertIn("PR #2 merged: B https://x/2", msg)
        self.assertEqual(msg.count("Done-means-live"), 1, "shared instruction must appear once, not per-PR")


class BatchByWindowTests(unittest.TestCase):
    def test_merges_within_60s_batch_together(self):
        records = [
            {"number": 1, "merged_at": "2026-09-24T12:00:00Z"},
            {"number": 2, "merged_at": "2026-09-24T12:00:30Z"},
        ]
        batches = hq.batch_by_window(records)
        self.assertEqual(len(batches), 1)
        self.assertEqual({r["number"] for r in batches[0]}, {1, 2})

    def test_merges_over_60s_apart_are_separate_batches(self):
        records = [
            {"number": 1, "merged_at": "2026-09-24T12:00:00Z"},
            {"number": 2, "merged_at": "2026-09-24T12:05:00Z"},
        ]
        batches = hq.batch_by_window(records)
        self.assertEqual(len(batches), 2)


class ReadNewRecordsTests(unittest.TestCase):
    def test_reads_only_lines_after_offset_and_returns_new_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "merged-prs.jsonl"
            offset = Path(tmp) / "merged-prs.jsonl.offset"
            jsonl.write_text(json.dumps({"number": 1}) + "\n")
            first_records, first_end = hq.read_new_records(jsonl, offset)
            self.assertEqual([r["number"] for r in first_records], [1])
            offset.write_text(str(first_end))

            with jsonl.open("a") as fh:
                fh.write(json.dumps({"number": 2}) + "\n")
            second_records, _ = hq.read_new_records(jsonl, offset)
            self.assertEqual([r["number"] for r in second_records], [2])

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "nope.jsonl"
            offset = Path(tmp) / "nope.jsonl.offset"
            records, end = hq.read_new_records(jsonl, offset)
            self.assertEqual(records, [])
            self.assertEqual(end, 0)


class MidTurnSkipsInjectionTests(unittest.TestCase):
    """The spec's own line: 'Don't inject while HQ is mid-turn if you can detect it.' A
    detected-busy run must send nothing and must not advance the offset file, so the deferred
    merge is re-read (not lost) on the next fire."""

    def test_mid_turn_detected_sends_nothing_and_leaves_offset_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "merged-prs.jsonl"
            offset = Path(tmp) / "merged-prs.jsonl.offset"
            jsonl.write_text(json.dumps({"number": 1, "title": "A", "url": "u", "merged_at": "2026-09-24T12:00:00Z"}) + "\n")

            with mock.patch.object(hq, "hq_mid_turn", return_value=True), \
                 mock.patch.object(hq, "send_to_hq") as send_mock, \
                 mock.patch.object(sys, "argv", ["notify_reif_hq.py", str(jsonl)]):
                hq.main()

            send_mock.assert_not_called()
            self.assertFalse(offset.exists(), "offset must not advance when a merge was deferred")

    def test_not_mid_turn_sends_and_advances_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "merged-prs.jsonl"
            offset = Path(tmp) / "merged-prs.jsonl.offset"
            jsonl.write_text(json.dumps({"number": 1, "title": "A", "url": "u", "merged_at": "2026-09-24T12:00:00Z"}) + "\n")

            with mock.patch.object(hq, "hq_mid_turn", return_value=False), \
                 mock.patch.object(hq, "send_to_hq") as send_mock, \
                 mock.patch.object(sys, "argv", ["notify_reif_hq.py", str(jsonl)]):
                hq.main()

            send_mock.assert_called_once()
            self.assertTrue(offset.exists())


if __name__ == "__main__":
    unittest.main()
