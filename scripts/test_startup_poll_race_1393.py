"""gh#1393, live on dino 2026-09-28: fleet.backlog_open, fleet.prs_open and
fleet.prs_open_over_4h read "stale · source never read" right after a container restart.

Root cause: main() started poll_gh_forever's background thread (which calls
`self.apply_gh(poll_gh_state())` -- merge_gh_poll -- every GH_POLL_S) and THEN ran its own
"warm the first page load" poll with a raw `STATE.gh = poll_gh_state()`. poll_gh_state() itself
never sets issues_at/prs_at/merged_at -- only merge_gh_poll (called from apply_gh) does. Both
polls do the same ~7 gh calls and take similar time, so it is a real race: if the raw
assignment's poll finishes last, it silently wipes the freshness keys the background thread's
own tick had just set, and every tile that reads them (metrics_snapshot -> gh.get("issues_at")
etc.) shows "source never read" until poll_gh_forever's NEXT tick lands, up to GH_POLL_S later.

RED without the fix: main()'s source does the raw assignment. GREEN with it: main() merges the
synchronous startup poll through apply_gh, exactly like every later tick, so the freshness keys
are always present the instant a poll completes -- no window where the page can catch it empty.

Run: python3 scripts/test_startup_poll_race_1393.py
"""
import inspect
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("FLEET_REPO", "")

import fleet_view_server as srv  # noqa: E402


class StartupPollMustMergeNotClobber(unittest.TestCase):
    def test_main_merges_the_startup_poll_instead_of_replacing_state_gh(self):
        src = inspect.getsource(srv.main)
        self.assertNotRegex(
            src, r"STATE\.gh\s*=\s*poll_gh_state\(\)",
            "a raw assignment here races poll_gh_forever's background thread and can wipe "
            "issues_at/prs_at/merged_at for a whole GH_POLL_S cycle after every restart")
        self.assertIn("STATE.apply_gh(poll_gh_state())", src,
                       "the startup poll must go through apply_gh so it merges via merge_gh_poll")

    def test_bare_poll_gh_state_has_no_freshness_keys(self):
        """Documents the mechanism: poll_gh_state() alone (what the old code assigned directly)
        never carries issues_at/prs_at/merged_at -- only merge_gh_poll adds those."""
        with unittest.mock.patch.object(srv, "_gh", return_value="[]"):
            raw = srv.poll_gh_state()
        for key in ("issues_at", "prs_at", "merged_at"):
            self.assertNotIn(key, raw, f"poll_gh_state() must not itself carry {key}")

    def test_apply_gh_on_a_fresh_state_sets_freshness_keys_immediately(self):
        """The fixed startup call: State().apply_gh(poll_gh_state()) must leave every tile
        reading a real as-of the instant the very first poll completes -- no empty window."""
        with unittest.mock.patch.object(srv, "_gh", return_value="[]"):
            state = srv.State()
            state.apply_gh(srv.poll_gh_state())
        self.assertIsNotNone(state.gh.get("issues_at"))
        self.assertIsNotNone(state.gh.get("prs_at"))
        self.assertIsNotNone(state.gh.get("merged_at"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
