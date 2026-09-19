"""fk#1146: an 'other' (unknown-cause) failure must never gate the LAST usable account.

On 2026-09-19 a generic `rc=1 reason=other` gated `tgp` while it held 84% of its week
unused. `philanthropy` was genuinely exhausted and `gmail` genuinely overdrawn, so
gating the one healthy account produced "ALL accounts in 'philanthropy tgp gmail'
failed this call" and the fleet built nothing for 95 minutes.

'other' means "we do not know why this failed" -- not "out of quota". Converting
*unknown* into *exhausted* spends a whole week of headroom on one bad minute.
"""
import os
import subprocess
import tempfile
import time
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def note_other_failure(log_dir, account, accounts, state_lines, times=1):
    """Drive _account_pool_note_other_failure `times` in ONE shell, so the streak file
    accumulates the way it does in a real run, then return (stdout, state-file text)."""
    state = os.path.join(log_dir, "account-pool-exhausted.state")
    with open(state, "w") as fh:
        fh.write("".join(state_lines))
    calls = "\n".join([f'_account_pool_note_other_failure "{account}"'] * times)
    script = f'set -u\n. "{POOL}"\n{calls}\n'
    # _account_pool_log appends to a FILE, never stdout, so the decision is read back
    # from that log rather than from the subprocess output.
    log = os.path.join(log_dir, "account-pool.log")
    env = dict(os.environ, FLEET_LOG_DIR=log_dir, FLEET_ACCOUNTS=accounts,
               ACCOUNT_POOL_LOG_FILE=log)
    env.pop("FLEET_MAXX_URL", None)
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    logged = open(log).read() if os.path.exists(log) else ""
    with open(state) as fh:
        return logged, fh.read()


class OtherGateLastAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.future = int(time.time()) + 3600

    def test_the_last_usable_account_is_not_gated(self):
        """THE MUTATION TARGET -- tonight's outage, replayed.

        philanthropy and gmail already gated; tgp is the only one left and takes 3
        consecutive 'other' failures. It must STAY in rotation."""
        out, state = note_other_failure(
            self.log_dir, "tgp", "philanthropy tgp gmail",
            [f"philanthropy {self.future} exhausted\n", f"gmail {self.future} exhausted\n"],
            times=3)
        self.assertNotIn("tgp", state.replace("philanthropy", ""),
                         f"tgp must not be written to the gate file. state={state!r}")
        self.assertIn("NOT gating", out, out)

    def test_it_still_gates_when_another_account_is_free(self):
        """The guard is narrow: with gmail healthy, gating tgp is correct -- the pool
        does not go empty, so the normal transient-failure behaviour stands."""
        out, state = note_other_failure(
            self.log_dir, "tgp", "philanthropy tgp gmail",
            [f"philanthropy {self.future} exhausted\n"],
            times=3)
        self.assertIn("tgp", state, f"tgp should be gated here. state={state!r}")
        self.assertIn("marked gated (other)", out, out)

    def test_an_expired_gate_counts_as_usable(self):
        """A gate whose epoch has passed is not a gate. If gmail's window has expired,
        tgp is not the last account and may be gated normally."""
        past = int(time.time()) - 60
        out, state = note_other_failure(
            self.log_dir, "tgp", "philanthropy tgp gmail",
            [f"philanthropy {self.future} exhausted\n", f"gmail {past} exhausted\n"],
            times=3)
        self.assertIn("tgp", state, f"gmail's gate expired, so tgp is gatable. state={state!r}")

    def test_below_threshold_still_does_not_gate(self):
        """Unchanged behaviour: one or two 'other' failures never gate, regardless."""
        out, state = note_other_failure(
            self.log_dir, "tgp", "philanthropy tgp gmail",
            [f"philanthropy {self.future} exhausted\n"],
            times=2)
        self.assertNotIn("tgp", state.replace("philanthropy", ""), state)
        self.assertIn("not gating yet", out, out)

    def test_a_single_account_pool_is_never_gated_on_other(self):
        """Degenerate but real: FLEET_ACCOUNTS with one entry. Gating it on an unknown
        cause switches the fleet off entirely."""
        out, state = note_other_failure(self.log_dir, "solo", "solo", [], times=3)
        self.assertNotIn("solo", state, state)
        self.assertIn("NOT gating", out, out)


if __name__ == "__main__":
    unittest.main()
