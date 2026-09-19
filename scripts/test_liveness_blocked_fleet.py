"""fk#1147: a fleet that is awake but not building pages on its own, fast.

On 2026-09-19 the fleet ran for 95 minutes without completing a single pass. Every
member woke, checked its allowance, went `paced`, and exited. Nothing alarmed:

  - not `silent`, because passes WERE running and the newest ok was inside the 3h
    threshold for most of it;
  - not `out_of_tokens`, because the pool's state file named no future reset.

A blocked fleet is louder than a silent one -- it reports a problem every few minutes --
so it must page on a short window rather than waiting out LIVENESS_MAX_AGE_S.
"""
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest

CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "member_liveness_check.sh")
BLOCKED = ("paced", "dispatch_skipped", "budget_declined", "killed")


def run_check(rows, *, max_age_s=10800, blocked_min=3, window_s=1800, exhausted=()):
    """`rows` is [(seconds_ago, member, status)]. Returns (stdout, pages)."""
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "fleet.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE runs (recorded_at REAL, member TEXT, status TEXT)")
    now = time.time()
    c.executemany("INSERT INTO runs VALUES (?,?,?)",
                  [(now - ago, m, s) for ago, m, s in rows])
    c.commit()
    c.close()

    if exhausted:
        with open(os.path.join(tmp, "account-pool-exhausted.state"), "w") as fh:
            for acct, epoch in exhausted:
                fh.write(f"{acct} {int(epoch)} exhausted\n")

    calls = os.path.join(tmp, "ntfy_calls")
    env = dict(os.environ, FLEET_LOG_DIR=tmp, FLEET_INSTANCE_NAME="testinst",
               NTFY_CALLS_FILE=calls, LIVENESS_MAX_AGE_S=str(max_age_s),
               LIVENESS_BLOCKED_MIN=str(blocked_min),
               LIVENESS_BLOCKED_WINDOW_S=str(window_s))
    out = subprocess.run(["bash", CHECK], capture_output=True, text=True, env=env).stdout
    pages = open(calls).read() if os.path.exists(calls) else ""
    return out, pages


class BlockedFleetTest(unittest.TestCase):
    def test_a_blocked_fleet_pages_without_waiting_for_silence(self):
        """THE MUTATION TARGET -- tonight, replayed.

        The newest ok is 40 min old, well inside the 3h silence threshold, and six
        passes have run and declined since. Before this, both branches stayed quiet."""
        rows = [(2400, "minion", "ok")] + [
            (t, m, "paced") for t, m in
            [(1500, "gru"), (1200, "judge-judy"), (900, "roomba"),
             (600, "gru"), (300, "judge-judy"), (120, "librarian-scrub")]]
        out, pages = run_check(rows)
        self.assertIn("PAGED blocked", out, out)
        self.assertIn("problem=blocked", pages, pages)
        self.assertIn("awake but not building", pages, pages)

    def test_every_declining_status_counts(self):
        """`paced` is only one way to decline. killed/budget_declined/dispatch_skipped
        are the same signal and were all present in the real outage."""
        for status in BLOCKED:
            rows = [(2400, "minion", "ok")] + [(t, "gru", status) for t in (900, 600, 300)]
            out, _ = run_check(rows)
            self.assertIn("PAGED blocked", out, f"{status} did not count as blocked: {out!r}")

    def test_a_working_fleet_does_not_page(self):
        """One ok in the window means it IS building. A paced pass beside real work is
        normal pacing, not an outage -- paging on it would train the alarm away."""
        rows = [(1500, "gru", "paced"), (900, "minion", "ok"),
                (600, "gru", "paced"), (300, "judge-judy", "paced")]
        out, pages = run_check(rows)
        self.assertIn("OK", out, out)
        self.assertNotIn("problem=blocked", pages, pages)

    def test_a_quiet_gap_does_not_page(self):
        """Nothing running at all, inside the silence threshold, is a quiet hour --
        the blocked branch must not fire on an empty window."""
        out, pages = run_check([(2400, "minion", "ok")])
        self.assertIn("OK", out, out)
        self.assertNotIn("problem=blocked", pages, pages)

    def test_below_the_minimum_does_not_page(self):
        """Two declines is a blip. The threshold exists so one bad minute is not an page."""
        rows = [(2400, "minion", "ok"), (600, "gru", "paced"), (300, "gru", "paced")]
        out, pages = run_check(rows)
        self.assertNotIn("problem=blocked", pages, pages)

    def test_a_genuine_quota_gap_still_reads_as_out_of_tokens(self):
        """A pool with a real future reset is the legitimate kind of down, and keeps its
        own problem key -- blocked must not swallow it."""
        rows = [(20000, "minion", "ok")] + [(t, "gru", "budget_declined") for t in (900, 600, 300)]
        out, pages = run_check(rows, exhausted=[("philanthropy", time.time() + 3600)])
        self.assertIn("blocked", out + pages,
                      "an all-declined window is still blocked even when a reset exists")

    def test_silence_still_pages_as_silence(self):
        """The original dead-man's switch is untouched: nothing at all, past the
        threshold, is still `silent` and still critical."""
        out, pages = run_check([(20000, "minion", "ok")], max_age_s=10800)
        self.assertIn("PAGED silent", out, out)
        self.assertIn("problem=silent", pages, pages)


if __name__ == "__main__":
    unittest.main()
