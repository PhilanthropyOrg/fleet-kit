"""fk#(pending): a never-anchored account's maxx reading (verdict=calibrating,
lifetime_billed=0, week_reset=null, week_bank_pct=null) must be synthesized into a real
week-reset cache line with full headroom -- NOT left unrecorded, which is what put it in
_account_pool_order's `unknown` bucket, sorting it LAST (behind every account with a real
reading, including a negative one).

Runs a tiny local HTTP server standing in for the maxx MCP endpoint (POST /mcp -> the same
JSON-RPC envelope maxx returns), so this exercises the REAL curl+parse code path in
account_pool.sh, not a re-implementation of it. test_account_pool_order.py deliberately never
curls (it seeds the cache file directly) -- this file is the missing live-fetch coverage for
the one branch that broke.
"""

import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def _mcp_response(budget: dict) -> bytes:
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(budget)}]}}
    return json.dumps(envelope).encode()


class _StubServer(http.server.BaseHTTPRequestHandler):
    budgets = {}  # handle -> budget dict, set per test

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        handle = parse_qs(urlparse(self.path).query).get("handle", [""])[0]
        body = _mcp_response(self.budgets.get(handle, {}))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence -- pytest captures enough noise already
        pass


class CalibratingUnbilledLiveFetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _StubServer)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _run_week_reset(self, tmp, account, budget):
        _StubServer.budgets = {account: budget}
        script = f'''set -u
. "{POOL}"
_account_pool_week_reset "{account}"
'''
        env = dict(
            os.environ,
            FLEET_LOG_DIR=tmp,
            FLEET_MAXX_URL=f"http://127.0.0.1:{self.port}",
            **{f"FLEET_MAXX_HANDLE_{account.upper()}": account,
               f"FLEET_MAXX_KEY_{account.upper()}": "test-key"},
        )
        out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        cache_path = os.path.join(tmp, "account-pool-exhausted.state.week-reset")
        cache = open(cache_path).read() if os.path.exists(cache_path) else ""
        return out.stdout.strip(), cache

    def test_never_billed_calibrating_account_gets_a_full_headroom_cache_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            epoch, cache = self._run_week_reset(
                tmp, "philanthropy",
                {"verdict": "calibrating", "lifetime_billed": 0, "week_reset": None, "week_bank_pct": None},
            )
            self.assertTrue(epoch, "expected a synthesized epoch, got nothing (still unrecorded)")
            self.assertIn("philanthropy", cache)
            self.assertIn(" 100", cache)  # bank=100 written as the 4th field

    def test_calibrating_account_with_real_spend_writes_no_cache_line(self):
        """The unsafe case (lifetime_billed > 0) must NOT be synthesized -- confirms the guard
        is lifetime_billed==0 specifically, not verdict==calibrating alone."""
        with tempfile.TemporaryDirectory() as tmp:
            epoch, cache = self._run_week_reset(
                tmp, "tgp",
                {"verdict": "calibrating", "lifetime_billed": 5000, "week_reset": None, "week_bank_pct": None},
            )
            self.assertEqual(epoch, "")
            self.assertNotIn("tgp", cache)

    def test_ordering_end_to_end_puts_the_unbilled_account_ahead_of_negative_banks(self):
        """THE incident, reproduced through _account_pool_order itself: philanthropy
        (never-billed, calibrating) must rank ABOVE gmail/tgp (both real negative banks) --
        not behind them in an `unknown` bucket."""
        with tempfile.TemporaryDirectory() as tmp:
            _StubServer.budgets = {
                "philanthropy": {"verdict": "calibrating", "lifetime_billed": 0, "week_reset": None, "week_bank_pct": None},
                "gmail": {"verdict": "ok", "lifetime_billed": 900000, "week_reset": 1790082000, "week_bank_pct": -5.7},
                "tgp": {"verdict": "ok", "lifetime_billed": 900000, "week_reset": 1790294400, "week_bank_pct": -12.8},
            }
            script = f'''set -u
. "{POOL}"
_account_pool_order
'''
            env = dict(
                os.environ,
                FLEET_LOG_DIR=tmp,
                FLEET_ACCOUNTS="gmail tgp philanthropy",
                FLEET_MAXX_URL=f"http://127.0.0.1:{self.port}",
                FLEET_MAXX_HANDLE_PHILANTHROPY="philanthropy", FLEET_MAXX_KEY_PHILANTHROPY="k",
                FLEET_MAXX_HANDLE_GMAIL="gmail", FLEET_MAXX_KEY_GMAIL="k",
                FLEET_MAXX_HANDLE_TGP="tgp", FLEET_MAXX_KEY_TGP="k",
            )
            out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            order = out.stdout.split()
            self.assertEqual(order[0], "philanthropy", f"expected philanthropy first, got order={order}")


if __name__ == "__main__":
    unittest.main()
