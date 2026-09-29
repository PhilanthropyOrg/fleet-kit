"""fk#1429: PAUSE/RESUME -- POST /api/members/<name>/pause makes run_member.sh skip that
member's scheduled AND woken passes (a run already in flight is untouched); RESUME clears it.

RED without it: member_pause.py does not exist, run_member.sh has no pause check at all (a
paused member's cron tick runs straight through to `claude -p`), and fleet_msg.wake() has no
"paused" reason (a paused member still gets woken by a `cause`/`command` message).

Run: python3 scripts/test_member_pause.py
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
RUN_MEMBER = KIT / "scripts" / "run_member.sh"
GHOST = "test-ghost-member"  # no members/<name>/ spec on purpose -- see class docstrings below


def _reimport(*names):
    for n in names:
        sys.modules.pop(n, None)


class MemberPauseModule(unittest.TestCase):
    """The file-flag primitive itself, isolated from run_member.sh/fleet_view_server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sys.path.insert(0, str(KIT / "scripts"))
        _reimport("member_pause")
        import member_pause
        self.mp = member_pause

    def tearDown(self):
        self.tmp.cleanup()

    def test_unpaused_member_reads_none(self):
        self.assertIsNone(self.mp.get("marie", self.tmp.name))

    def test_pause_then_get_round_trips(self):
        rec = self.mp.pause("marie", "reif", log_dir=self.tmp.name, now=1000.0)
        self.assertEqual(rec["paused"], True)
        self.assertEqual(rec["by"], "reif")
        got = self.mp.get("marie", self.tmp.name)
        self.assertEqual(got["since"], 1000.0)
        self.assertEqual(got["by"], "reif")

    def test_resume_clears_it(self):
        self.mp.pause("marie", "reif", log_dir=self.tmp.name)
        self.assertTrue(self.mp.resume("marie", log_dir=self.tmp.name))
        self.assertIsNone(self.mp.get("marie", self.tmp.name))

    def test_resume_on_an_unpaused_member_is_a_no_op(self):
        self.assertFalse(self.mp.resume("marie", log_dir=self.tmp.name))

    def test_pause_is_scoped_per_member(self):
        self.mp.pause("marie", "reif", log_dir=self.tmp.name)
        self.assertIsNone(self.mp.get("gru", self.tmp.name))


class WakeSkipsAPausedMember(unittest.TestCase):
    """fleet_msg.wake() must never launch a paused member, same chokepoint it already uses for
    disabled/hq/no-spec (test_fleet_msg_wake.py's own DisabledMemberNotWoken class)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name) / "logs"
        sys.path.insert(0, str(KIT / "scripts"))
        _reimport("fleet_db", "fleet_msg", "member_pause")
        import fleet_db
        import fleet_msg
        import member_pause
        self.fleet_db, self.fleet_msg, self.mp = fleet_db, fleet_msg, member_pause
        self.conn = fleet_db.connect(Path(self.tmp.name) / "fleet.db")
        self.launched = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_paused_recipient_is_not_woken(self):
        specs = {"marie": {"name": "marie", "enabled": True}}
        self.mp.pause("marie", "reif", log_dir=self.log_dir, now=1000.0)
        sent = self.fleet_msg.send(self.conn, "reif", "marie", "command", "k1", "do it", now=1000.0)
        out = self.fleet_msg.wake(self.conn, sent, "command", specs=specs, now=1000.0,
                                  launcher=lambda m, r: self.launched.append((m, r)),
                                  log_dir=self.log_dir)
        self.assertEqual(self.launched, [])
        self.assertFalse(out[0]["woken"])
        self.assertEqual(out[0]["wake_reason"], "paused")

    def test_resumed_recipient_is_woken_again(self):
        specs = {"marie": {"name": "marie", "enabled": True}}
        self.mp.pause("marie", "reif", log_dir=self.log_dir, now=1000.0)
        self.mp.resume("marie", log_dir=self.log_dir)
        sent = self.fleet_msg.send(self.conn, "reif", "marie", "command", "k1", "do it", now=1000.0)
        out = self.fleet_msg.wake(self.conn, sent, "command", specs=specs, now=1000.0,
                                  launcher=lambda m, r: self.launched.append((m, r)),
                                  log_dir=self.log_dir)
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(out[0]["woken"])


class RunMemberShSkipsAPausedDispatch(unittest.TestCase):
    """Drives the real script. GHOST has no members/<name>/ spec, which is fine -- the pause
    check sits before spec resolution (asserted by RunMemberShOrdering below), so a paused
    dispatch never reaches the point that would need one. An UNPAUSED ghost member proceeds
    past the pause check and fails at spec resolution (exit 2, "no such fleet member") instead
    of at the pause check (exit 0, a `paused` record) -- that difference is what proves the
    pause branch was actually skipped, without this test ever needing a real `claude` binary.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-pause-test-")
        self.log_dir = Path(self.tmp) / "logs"
        self.log_dir.mkdir()
        self.env = dict(os.environ, FLEET_REPO="test/repo", FLEET_LOG_DIR=str(self.log_dir),
                        FLEET_ENV_FILE="/dev/null")

    def _run(self, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run(["bash", str(RUN_MEMBER), GHOST], env=env,
                              capture_output=True, text=True, timeout=30)

    def _runs_jsonl(self) -> str:
        p = self.log_dir / "runs.jsonl"
        return p.read_text() if p.exists() else ""

    def test_unpaused_dispatch_is_not_skipped_for_pause(self):
        r = self._run()
        self.assertEqual(r.returncode, 2, r.stderr)  # fails downstream, at spec resolution
        self.assertNotIn("paused (by", self._runs_jsonl())

    def test_paused_dispatch_exits_clean_and_records_the_skip(self):
        sys.path.insert(0, str(KIT / "scripts"))
        _reimport("member_pause")
        import member_pause
        member_pause.pause(GHOST, "reif (fleet-view)", log_dir=self.log_dir)

        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = self._runs_jsonl()
        self.assertIn(GHOST, runs)
        self.assertIn("paused", runs)
        self.assertIn("reif (fleet-view)", runs)

    def test_run_now_bypasses_pause(self):
        sys.path.insert(0, str(KIT / "scripts"))
        _reimport("member_pause")
        import member_pause
        member_pause.pause(GHOST, "reif", log_dir=self.log_dir)

        r = self._run(extra_env={"FLEET_RUN_NOW": "1"})
        self.assertEqual(r.returncode, 2, r.stderr)  # proceeded past pause, failed downstream
        self.assertNotIn("paused (by", self._runs_jsonl())


class RunMemberShOrdering(unittest.TestCase):
    def test_pause_check_runs_before_the_dispatch_lock_and_spec_resolution(self):
        text = RUN_MEMBER.read_text()
        pause_pos = text.index("member_pause")
        lock_pos = text.index("per-member dispatch lock")
        spec_pos = text.index("resolve spec: git baseline")
        self.assertLess(pause_pos, lock_pos)
        self.assertLess(pause_pos, spec_pos)


class HttpPauseResumeRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name) / "logs"
        self.log_dir.mkdir()
        self.env_file = Path(self.tmp.name) / "fleet.env"
        self.env_file.write_text("")
        self._old_env = {k: os.environ.get(k) for k in
                         ("FLEET_LOG_DIR", "FLEET_ENV_FILE", "FLEET_REPO", "FLEET_DB_PATH")}
        os.environ["FLEET_LOG_DIR"] = str(self.log_dir)
        os.environ["FLEET_ENV_FILE"] = str(self.env_file)
        os.environ["FLEET_REPO"] = "test/repo"
        os.environ.pop("FLEET_DB_PATH", None)

        sys.path.insert(0, str(KIT / "scripts"))
        _reimport("fleet_view_server", "fleet_db", "fleet_msg", "member_pause", "open_runs")
        import fleet_view_server as fvs
        self.fvs = fvs
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fvs.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def test_pause_then_ping_shows_paused_since_by(self):
        status, body = self._post("/api/members/marie/pause", {"by": "reif"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, ping = self._get("/api/members/marie/ping")
        self.assertTrue(ping["paused"])
        self.assertEqual(ping["paused_by"], "reif")
        self.assertIsNotNone(ping["paused_since"])

    def test_resume_clears_it(self):
        self._post("/api/members/marie/pause", {"by": "reif"})
        status, body = self._post("/api/members/marie/resume", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["resumed"])
        status, ping = self._get("/api/members/marie/ping")
        self.assertFalse(ping["paused"])
        self.assertIsNone(ping["paused_by"])

    def test_unknown_member_pause_is_400(self):
        status, body = self._post("/api/members/nonexistent-ghost/pause", {})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
