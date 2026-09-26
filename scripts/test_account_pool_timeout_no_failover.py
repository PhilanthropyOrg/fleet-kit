"""A pass that hits its own timeout (rc=124) must not be re-run on the next account.

2026-09-24 16:44Z, ad-hoc sentry pass: `timeout 900 claude -p` expired on philanthropy
(15:58 -> 16:13), the pool classified rc=124 as 'other' and re-ran the WHOLE pass on gmail
(-> 16:29), then tgp (-> 16:44), then reported "ALL accounts ... failed this call" -> rc=3,
recorded as `budget_declined (account=none reason=other)`. Three full passes of spend, zero
output, and the real cause (the member's timeout_s is shorter than its job) hidden behind a
budget label. A timeout is the member's own clock, not an account signal: return 124 from
the account that ran it so run_member.sh records `timed_out`.
"""
import os
import subprocess
import tempfile
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def run_pool(log_dir, accounts, command):
    calls = os.path.join(log_dir, "calls")
    selected = os.path.join(log_dir, "selected")
    reason = os.path.join(log_dir, "reason")
    script = (f'set -u\n. "{POOL}"\n'
              f'account_pool_run bash -c \'echo "$CLAUDE_CONFIG_DIR" >> "{calls}"; {command}\'\n'
              'echo "RC=$?"\n')
    env = dict(os.environ, FLEET_LOG_DIR=log_dir, FLEET_ACCOUNTS=accounts,
               ACCOUNT_POOL_LOG_FILE=os.path.join(log_dir, "account-pool.log"),
               ACCOUNT_POOL_SELECTED_FILE=selected, ACCOUNT_POOL_REASON_FILE=reason,
               HOME=log_dir)
    for k in ("FLEET_MAXX_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(k, None)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    read = lambda p: open(p).read().strip() if os.path.exists(p) else ""  # noqa: E731
    return out, read(calls).splitlines(), read(selected), read(reason)


class TimeoutNoFailoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_timeout_is_not_retried_on_the_next_account(self):
        """THE MUTATION TARGET -- the 16:44Z run, replayed with a 3-account pool."""
        out, calls, selected, reason = run_pool(self.tmp.name, "philanthropy gmail tgp", "exit 124")
        self.assertEqual(len(calls), 1, f"timed-out pass re-ran on {len(calls)} accounts: {calls}")
        self.assertIn("RC=124", out, out)
        self.assertEqual(reason, "timeout")
        self.assertTrue(selected, "the account that ran the pass must be recorded, not 'none'")

    def test_a_killed_pass_is_not_retried_or_gated(self):
        """2026-09-26 13:29:40: a SIGTERM (rc=143) marked philanthropy unauthenticated and re-ran gru on tgp."""
        for rc in (143, 137):
            out, calls, selected, reason = run_pool(self.tmp.name, "philanthropy gmail tgp", f"exit {rc}")
            self.assertEqual(len(calls), 1, f"killed pass re-ran on {len(calls)} accounts: {calls}")
            self.assertIn(f"RC={rc}", out, out)
            self.assertEqual(reason, "killed")
            os.remove(os.path.join(self.tmp.name, "calls"))

    def test_a_real_other_failure_still_fails_over(self):
        """Unchanged: a non-timeout unknown failure still tries the next account."""
        out, calls, _, _ = run_pool(self.tmp.name, "philanthropy gmail", "exit 1")
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("RC=3", out, out)


if __name__ == "__main__":
    unittest.main()
