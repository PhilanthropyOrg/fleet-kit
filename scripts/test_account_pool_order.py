"""fk#1136: among healthy accounts the pool tries the one with the MOST week-bank headroom
first; soonest weekly reset is only the tie-break. Before this, a drained account whose week
reset came sooner was tried (and paced against) ahead of a fresh one."""
import os
import subprocess
import tempfile
import time
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def order(log_dir, cache_lines, accounts="gmail tgp philanthropy", state_lines=()):
    cache = os.path.join(log_dir, "account-pool-exhausted.state.week-reset")
    with open(cache, "w") as fh:
        fh.write("".join(cache_lines))
    with open(os.path.join(log_dir, "account-pool-exhausted.state"), "w") as fh:
        fh.write("".join(state_lines))
    script = f'set -u\n. "{POOL}"\n_account_pool_order\n'
    env = dict(os.environ, FLEET_LOG_DIR=log_dir, FLEET_ACCOUNTS=accounts,
               ACCOUNT_POOL_WEEK_RESET_TTL="3600")
    env.pop("FLEET_MAXX_URL", None)  # never curl in a test; the cache is the only source
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


class OrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = int(time.time())

    def test_most_headroom_first_even_when_it_resets_later(self):
        # the live 2026-09-17 shape: gmail resets sooner but is drained; tgp is fresh
        got = order(self.tmp.name, [
            f"gmail {self.now + 380000} {self.now} -32.9\n",
            f"tgp {self.now + 593000} {self.now} 1.0\n",
            f"philanthropy {self.now + 186000} {self.now} -30.8\n",
        ])
        self.assertEqual(got, ["tgp", "philanthropy", "gmail"])

    def test_equal_banks_fall_back_to_soonest_reset(self):
        got = order(self.tmp.name, [
            f"gmail {self.now + 380000} {self.now} 5\n",
            f"tgp {self.now + 593000} {self.now} 5\n",
        ], accounts="tgp gmail")
        self.assertEqual(got, ["gmail", "tgp"])

    def test_unreadable_bank_sorts_after_every_reading_by_soonest_reset(self):
        got = order(self.tmp.name, [
            f"gmail {self.now + 380000} {self.now}\n",          # old 3-field cache line
            f"tgp {self.now + 593000} {self.now} -40\n",
        ], accounts="gmail tgp")
        self.assertEqual(got, ["tgp", "gmail"])


    def test_lapsed_gate_with_a_bank_reading_sorts_by_bank_not_first(self):
        # fk#1174, the live 2026-09-19 20:41Z shape: gmail's 'other' gate expired at 18:33Z and
        # its row was still in the state file; its bank is -23 and tgp's is -10. Before the fix
        # the lapsed row put gmail first and every member paced against its 0.0000 ceiling.
        got = order(self.tmp.name, [
            f"gmail {self.now + 380000} {self.now} -23.2\n",
            f"tgp {self.now + 593000} {self.now} -10.4\n",
        ], accounts="gmail tgp", state_lines=[f"gmail {self.now - 8000} other\n"])
        self.assertEqual(got, ["tgp", "gmail"])
        # A lapsed gate with NO reading keeps gh#462's freshest-first rule.
        got = order(self.tmp.name, [
            f"tgp {self.now + 593000} {self.now} -10.4\n",
        ], accounts="gmail tgp", state_lines=[f"gmail {self.now - 8000}\n"])
        self.assertEqual(got, ["gmail", "tgp"])


if __name__ == "__main__":
    unittest.main()
