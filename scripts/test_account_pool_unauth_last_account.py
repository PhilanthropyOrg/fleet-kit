"""PHIA-3803: one 'unauthenticated' read must not take the LAST usable account out of the pool,
and a transcript that merely quotes a 401 is not a login failure.

11-09 18:16 CST: gmail and tgp were at 100% of the week, philanthropy was the only login left.
One 'unauthenticated' verdict gated it for an hour ("gated:unauthenticated until 01:16Z"); every
pass logged "ALL accounts ... failed" and judge-judy posted no verdict for ~40 minutes.
"""
import os
import subprocess
import tempfile
import time
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def classify(text, rc=1):
    script = f'. "{POOL}"\n_account_pool_classify_failure "$1" "$2"\n'
    return subprocess.run(["bash", "-c", script, "x", text, str(rc)], capture_output=True,
                          text=True).stdout.strip()


def note_unauth(log_dir, account, accounts, state_lines):
    state = os.path.join(log_dir, "account-pool-exhausted.state")
    with open(state, "w") as fh:
        fh.write("".join(state_lines))
    log = os.path.join(log_dir, "account-pool.log")
    env = dict(os.environ, FLEET_LOG_DIR=log_dir, FLEET_ACCOUNTS=accounts, ACCOUNT_POOL_LOG_FILE=log)
    env.pop("FLEET_MAXX_URL", None)
    subprocess.run(["bash", "-c", f'. "{POOL}"\n_account_pool_note_unauthenticated "{account}"\n'],
                   capture_output=True, text=True, env=env)
    logged = open(log).read() if os.path.exists(log) else ""
    return logged, open(state).read()


class UnauthLastAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.future = int(time.time()) + 3600

    def test_the_last_usable_account_is_not_gated(self):
        out, state = note_unauth(
            self.tmp.name, "philanthropy", "philanthropy gmail tgp",
            [f"gmail {self.future} exhausted\n", f"tgp {self.future} exhausted\n"])
        self.assertNotIn("philanthropy", state, state)
        self.assertIn("NOT gating", out, out)

    def test_a_single_account_pool_still_gates_for_readiness(self):
        """gh#134: a logged-out sole account must read gated so readiness shows the outage."""
        out, state = note_unauth(self.tmp.name, "acct", "acct", [])
        self.assertIn("unauthenticated", state, state)

    def test_it_still_gates_when_another_account_is_free(self):
        out, state = note_unauth(
            self.tmp.name, "philanthropy", "philanthropy gmail tgp",
            [f"gmail {self.future} exhausted\n"])
        self.assertIn("philanthropy", state, state)
        self.assertIn("unauthenticated", state, state)


class ClassifyTest(unittest.TestCase):
    def test_a_long_transcript_that_quotes_a_401_is_other(self):
        work = "reviewed the diff, ran the tests\n" * 40
        self.assertEqual(classify(work + "curl returned HTTP 401 Unauthorized\nfinal: done\n"), "other")

    def test_a_real_login_failure_is_still_unauthenticated(self):
        self.assertEqual(classify("Invalid API key · Please run /login"), "unauthenticated")
        self.assertEqual(classify("Not logged in · Please run /login"), "unauthenticated")
        self.assertEqual(classify('API Error: 401 {"error":{"type":"authentication_error"}}'),
                         "unauthenticated")

    def test_a_login_failure_after_a_long_preamble_is_still_found(self):
        self.assertEqual(classify('{"type":"system"}\n' * 80 + "Not logged in · Please run /login"),
                         "unauthenticated")


if __name__ == "__main__":
    unittest.main()
