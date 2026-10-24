"""A dropped connection must not gate an account (fk#1752).

2026-10-23 20:42 CDT, DNS down on the host: gmail's pass failed rc=1 and was classified
`unauthenticated` ("needs a human to re-auth"), gating it for 6h; the 10-20 Wi-Fi outage did
the same to all three accounts. A bare `401` in the auth pattern also matched issue numbers
like #13401 anywhere in the member's transcript.
"""
import os
import subprocess
import tempfile
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def classify(text):
    script = f'. "{POOL}"\n_account_pool_classify_failure "$1" 1 subscription\n'
    return subprocess.run(["bash", "-c", script, "x", text], capture_output=True, text=True).stdout.strip()


class NetworkNotAuthTest(unittest.TestCase):
    def test_connection_errors_classify_as_network(self):
        for words in ("API Error: Connection dropped (ECONNRESET)", "No response from API",
                      "[Errno -3] Temporary failure in name resolution", "getaddrinfo EAI_AGAIN api.anthropic.com"):
            self.assertEqual(classify(words), "network", words)

    def test_issue_number_with_401_is_not_a_login_failure(self):
        self.assertEqual(classify("commented on #13401 and #4012"), "other")
        self.assertEqual(classify("HTTP 401 Unauthorized"), "unauthenticated")
        self.assertEqual(classify("Not logged in. Please run /login"), "unauthenticated")

    def test_a_real_limit_still_wins(self):
        self.assertEqual(classify("You've hit your weekly limit. ECONNRESET"), "exhausted")

    def test_network_failure_gates_nothing_and_does_not_fail_over(self):
        with tempfile.TemporaryDirectory() as d:
            calls = os.path.join(d, "calls")
            script = (f'. "{POOL}"\naccount_pool_run bash -c \'echo x >> "{calls}"; '
                      'echo "API Error: Connection dropped (ECONNRESET)"; exit 1\'\necho "RC=$?"\n')
            env = dict(os.environ, FLEET_LOG_DIR=d, FLEET_ACCOUNTS="philanthropy gmail tgp", HOME=d,
                       ACCOUNT_POOL_LOG_FILE=os.path.join(d, "account-pool.log"),
                       ACCOUNT_POOL_REASON_FILE=os.path.join(d, "reason"))
            for k in ("FLEET_MAXX_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
                env.pop(k, None)
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
            self.assertIn("RC=1", out, out)
            self.assertEqual(len(open(calls).read().splitlines()), 1, "a network failure re-ran on the next account")
            self.assertEqual(open(os.path.join(d, "reason")).read().strip(), "network")
            state = os.path.join(d, "account-pool-exhausted.state")
            self.assertFalse(os.path.exists(state) and open(state).read().strip(), "a network failure gated an account")


if __name__ == "__main__":
    unittest.main()
