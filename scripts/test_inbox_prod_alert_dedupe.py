#!/usr/bin/env python3
"""Test that prod alert issues are deduplicated by signature, not exact title.

fk#1129 fix: prod alerts with dynamic identifiers (e.g., pg_lock:<pid>) should
comment on existing issues instead of creating twins.

Run: python3 scripts/test_inbox_prod_alert_dedupe.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import inbox  # noqa: E402
import issue_cluster  # noqa: E402


class ProdAlertDedupeTests(unittest.TestCase):
    def test_prod_alert_with_dynamic_id_dedupes_by_signature(self):
        """Two prod alerts with the same root cause but different ids (e.g., different pids)
        should comment on the existing issue, not create a twin."""
        # Mock open issues: one existing prod alert
        existing = {
            "number": 100,
            "title": "prod alert [pg_lock:1234567:pg_toast_16627]",
            "url": "https://github.com/org/repo/issues/100",
        }

        def mock_open_issue_titled(slug, title, run, by_signature=False):
            """Mock that returns the existing issue when searching by signature."""
            if by_signature and title.startswith("prod alert ["):
                sig_want = issue_cluster.signature(title)
                sig_have = issue_cluster.signature(existing["title"])
                if sig_want == sig_have:
                    return existing
            return None

        def mock_run(cmd):
            """Mock gh commands for testing."""
            class Result:
                def __init__(self, rc, out):
                    self.returncode = rc
                    self.stdout = out
                    self.stderr = ""

            cmd_str = " ".join(cmd)
            # Mock issue comment
            if "issue comment" in cmd_str and "100" in cmd_str:
                return Result(0, "")
            # Mock issue list (shouldn't be called with our fix)
            if "issue list" in cmd_str:
                return Result(0, json.dumps([existing]))
            return Result(0, "https://github.com/org/repo/issues/100")

        def mock_repo_slug():
            return "org/repo"

        env = {"FLEET_MEMBER": "prod-monitor", "FLEET_REPO_URL": "https://github.com/org/repo"}
        with unittest.mock.patch.object(inbox, "open_issue_titled", mock_open_issue_titled), \
             unittest.mock.patch.object(inbox, "repo_slug", mock_repo_slug), \
             unittest.mock.patch.dict(os.environ, env):
            # File two prod alerts with different ids but same root cause
            title1 = "prod alert [pg_lock:1234567:pg_toast_16627]"
            title2 = "prod alert [pg_lock:9999999:pg_toast_16627]"

            # First one creates a new issue (no existing mock for this)
            # Second one should find and comment on the existing one
            result = inbox.file_backlog(title2, "test body", "scanner@example.com", run=mock_run)

            # Result should be the existing issue URL
            self.assertIn("100", result)

    def test_prod_alert_signature_drops_numbers_and_parentheticals(self):
        """Signatures for prod alerts should normalize by dropping numbers and parentheticals."""
        titles = [
            "prod alert [pg_lock:1234567:pg_toast_16627]",
            "prod alert [pg_lock:9999999:pg_toast_16627]",
            "prod alert [pg_lock:555:pg_toast_16627]",
        ]
        sigs = [issue_cluster.signature(t) for t in titles]
        # All should have the same signature
        self.assertEqual(len(set(sigs)), 1, f"Expected all to normalize to one signature, got {sigs}")
        # Signature should not contain the pid numbers
        self.assertNotIn("1234567", sigs[0])
        self.assertNotIn("9999999", sigs[0])

    def test_non_prod_alert_dedupes_by_exact_title(self):
        """Non-prod-alert backlog items should use exact title matching, not signature."""
        # Mock that returns None (no existing issue)
        def mock_open_issue_titled(slug, title, run, by_signature=False):
            # Should be called with by_signature=False for non-prod-alerts
            self.assertFalse(by_signature, "Non-prod-alert should not use by_signature=True")
            return None

        def mock_run(cmd):
            """Mock gh for issue creation."""
            class Result:
                def __init__(self, rc, out):
                    self.returncode = rc
                    self.stdout = out
                    self.stderr = ""
            # Return a mock issue URL
            return Result(0, "https://github.com/org/repo/issues/200\n")

        def mock_repo_slug():
            return "org/repo"

        env = {"FLEET_REPO_URL": "https://github.com/org/repo"}
        with unittest.mock.patch.object(inbox, "open_issue_titled", mock_open_issue_titled), \
             unittest.mock.patch.object(inbox, "repo_slug", mock_repo_slug), \
             unittest.mock.patch.dict(os.environ, env):
            # File a non-prod-alert backlog item
            inbox.file_backlog("backlog: fix the button", "test body", "user@example.com", run=mock_run)


if __name__ == "__main__":
    unittest.main()
