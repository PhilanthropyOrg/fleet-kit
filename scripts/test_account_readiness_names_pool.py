"""fk#1148: the capacity meter must not quietly report on a pool it invented.

`FLEET_ACCOUNTS` unset used to fall back to the single name "primary", so running this
without the instance env printed a confident `ready=1 total=1 gated=` for a THREE-account
pool. That reads exactly like a healthy one-account pool.

During the 2026-09-19 outage this was the first line read when asking "does the fleet have
capacity", and it answered "everything is fine" while two of three accounts were gated and
the fleet had built nothing for 95 minutes.
"""
import os
import subprocess
import tempfile
import time
import unittest

CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_readiness.sh")


def readiness(accounts, state_lines=(), unset=False):
    tmp = tempfile.mkdtemp()
    state = os.path.join(tmp, "account-pool-exhausted.state")
    with open(state, "w") as fh:
        fh.write("".join(state_lines))
    env = dict(os.environ, FLEET_LOG_DIR=tmp, ACCOUNT_POOL_STATE_FILE=state)
    if unset:
        env.pop("FLEET_ACCOUNTS", None)
    else:
        env["FLEET_ACCOUNTS"] = accounts
    r = subprocess.run(["bash", CHECK], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


class ReadinessNamesThePoolTest(unittest.TestCase):
    def setUp(self):
        self.future = int(time.time()) + 3600

    def test_an_unset_pool_is_a_loud_error_not_a_confident_one(self):
        """THE MUTATION TARGET. Never invent a pool: a plausible `ready=1 total=1` is
        worse than no answer, because a reader cannot tell it apart from the truth."""
        rc, out, err = readiness("", unset=True)
        self.assertEqual(rc, 2, f"unset FLEET_ACCOUNTS must fail loudly. rc={rc} out={out!r}")
        self.assertNotIn("ready=1 total=1", out + err,
                         "must not report a fabricated single-account pool")
        self.assertIn("FLEET_ACCOUNTS", err, err)

    def test_it_names_which_accounts_are_ready(self):
        """Counts alone cannot be diffed against FLEET_ACCOUNTS by eye: `ready=2 total=3`
        does not say WHICH two, so a reader cannot spot the wrong pair."""
        rc, out, _ = readiness(
            "philanthropy tgp gmail",
            [f"philanthropy {self.future} exhausted\n"])
        self.assertIn("ready=2 total=3", out, out)
        self.assertIn("ready_names=tgp,gmail", out, out)
        self.assertIn("gated=philanthropy:exhausted", out, out)

    def test_the_real_outage_reads_correctly(self):
        """2026-09-19: philanthropy exhausted, tgp gated on an unknown cause, gmail the
        only one left. The line must name all three states, not just count them."""
        rc, out, _ = readiness(
            "philanthropy tgp gmail",
            [f"philanthropy {self.future} exhausted\n", f"tgp {self.future} other\n"])
        self.assertIn("ready=1 total=3", out, out)
        self.assertIn("ready_names=gmail", out, out)
        self.assertIn("philanthropy:exhausted", out, out)
        self.assertIn("tgp:other", out, out)

    def test_a_fully_gated_pool_still_exits_nonzero(self):
        """Unchanged contract: callers do `account_readiness.sh || skip`."""
        rc, out, _ = readiness(
            "a b",
            [f"a {self.future} exhausted\n", f"b {self.future} other\n"])
        self.assertEqual(rc, 1, out)
        self.assertIn("ready=0 total=2", out, out)
        self.assertIn("ready_names=", out, out)

    def test_an_expired_gate_is_not_a_gate(self):
        past = int(time.time()) - 60
        rc, out, _ = readiness("a b", [f"a {past} exhausted\n"])
        self.assertEqual(rc, 0, out)
        self.assertIn("ready=2 total=2", out, out)
        self.assertIn("ready_names=a,b", out, out)

    def test_a_healthy_pool_exits_zero(self):
        rc, out, _ = readiness("philanthropy tgp gmail")
        self.assertEqual(rc, 0, out)
        self.assertIn("ready=3 total=3", out, out)
        self.assertIn("gated=", out, out)


if __name__ == "__main__":
    unittest.main()
