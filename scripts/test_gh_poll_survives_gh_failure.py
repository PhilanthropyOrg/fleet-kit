"""A `gh` outage on the host must not stomp the dashboard's last-good PR snapshot with a false
all-zero one.

Live on dino, 2026-09-26: the host ran at load 30 on 7 cores, every `gh` call in poll_gh_state()
took 70-85s against _gh()'s 15s timeout, and _gh() swallows a timeout into "". Each tick then
replaced the snapshot with empty lists, and fleet_home read "Nothing merged yet today" on a day
the fleet merged 23 PRs.

RED without the fix: the poll loop assigns every tick's result, so the failed tick erases the
merged list. GREEN with it: a tick where every call failed keeps the prior data and only flips
`ok`/`error`, so the page can say "gh unavailable" instead of reporting a zero.
"""
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("FLEET_REPO", "")

import fleet_view_server as srv  # noqa: E402


class GhPollSurvivesGhFailure(unittest.TestCase):
    def test_failed_tick_keeps_last_good_merged_list(self):
        state = srv.State()
        with unittest.mock.patch.object(srv, "_gh", return_value='[{"number": 1323, "mergedAt": "2026-09-26T21:28:47Z"}]'):
            state.apply_gh(srv.poll_gh_state())
        self.assertEqual([pr["number"] for pr in state.gh["merged"]], [1323])

        # Every gh call fails this tick (timeout/auth/network all look like "" from _gh).
        with unittest.mock.patch.object(srv, "_gh", return_value=""):
            state.apply_gh(srv.poll_gh_state())

        self.assertEqual([pr["number"] for pr in state.gh["merged"]], [1323],
                         "a failed gh tick must not erase the last real merged-PR list")
        self.assertIs(state.gh["ok"], False, "the page must be able to tell it's looking at stale data")

    def test_successful_tick_after_failure_clears_the_flag(self):
        state = srv.State()
        with unittest.mock.patch.object(srv, "_gh", return_value=""):
            state.apply_gh(srv.poll_gh_state())
        with unittest.mock.patch.object(srv, "_gh", return_value="[]"):
            state.apply_gh(srv.poll_gh_state())
        self.assertIs(state.gh["ok"], True, "a real empty result is not an outage")


class GhPollFitsTheSharedBudget(unittest.TestCase):
    def test_default_poll_stays_under_a_tenth_of_the_hourly_graphql_budget(self):
        """One poll measured at up to 18 GraphQL points (2026-09-26). Every member shares the
        account's 5,000/hr; at the old 20s default the dashboard spent half of it alone."""
        self.assertLessEqual(3600 / srv.GH_POLL_S * 18, 500)


if __name__ == "__main__":
    unittest.main()
