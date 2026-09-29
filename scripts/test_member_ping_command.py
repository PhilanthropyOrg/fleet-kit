"""fk#1429: agents can be pinged -- GET .../ping returns 200 with liveness, POST .../command
writes a fleet_msg and wakes the recipient, GET /api/messages/<id> reads back the
open -> understood -> done state a command goes through.

RED without the routes: GET /api/members/marie/ping and POST /api/members/marie/command both
404 on main (no such path is matched in do_GET/do_POST), and fleet_msg.py has no `done` verb or
`understood` status at all -- ack always went straight to the terminal `acked` state.

Run: python3 scripts/test_member_ping_command.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
MEMBER = "marie"  # real member/marie/marie.fleet.json: enabled=true, hourly_at_minute=33


def _reimport(*names):
    for n in names:
        sys.modules.pop(n, None)


class Base(unittest.TestCase):
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

        # No real subprocess ever leaves this test: fleet_msg.wake()'s default launcher Popens
        # `bash run_member.sh <member>` (a real fleet pass) -- swap it for a recorder.
        self.spawned = []
        patcher = unittest.mock.patch.object(
            fvs.fleet_msg.subprocess, "Popen",
            side_effect=lambda cmd, **kw: self.spawned.append((cmd, kw)))
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def _write_run(self, member, run_id, status, ts, **extra):
        rec = {"ts": ts, "member": member, "run_id": run_id, "status": status, **extra}
        with (self.log_dir / "runs.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")


class Ping(Base):
    def test_unknown_member_is_404(self):
        status, body = self._get("/api/members/nonexistent-ghost/ping")
        self.assertEqual(status, 404)

    def test_idle_member_ping_shape(self):
        status, body = self._get(f"/api/members/{MEMBER}/ping")
        self.assertEqual(status, 200)
        self.assertEqual(body["member"], MEMBER)
        self.assertTrue(body["enabled"])
        self.assertFalse(body["running"])
        self.assertIsNone(body["running_since"])
        self.assertIsNone(body["last_run"])
        self.assertIsNotNone(body["next_due"], "marie has hourly_at_minute=33 -- next_due "
                                                "should be cheaply computable")
        self.assertEqual(body["inbox_open"], 0)
        self.assertFalse(body["paused"])

    def test_ping_reports_a_live_run(self):
        self._write_run(MEMBER, "marie-123-999", "started", time.time() - 30)
        status, body = self._get(f"/api/members/{MEMBER}/ping")
        self.assertTrue(body["running"])
        self.assertIsNotNone(body["running_since"])

    def test_ping_reports_last_completed_run(self):
        self._write_run(MEMBER, "marie-1-1", "started", time.time() - 600)
        self._write_run(MEMBER, "marie-1-1", "success", time.time() - 500, exit_code=0)
        status, body = self._get(f"/api/members/{MEMBER}/ping")
        self.assertFalse(body["running"])
        self.assertEqual(body["last_run"]["status"], "success")

    def test_stale_started_row_does_not_read_as_running(self):
        # older than open_runs.MAX_AGE_S -- a run that died without a terminal row, not a live one
        self._write_run(MEMBER, "marie-old-1", "started",
                        time.time() - self.fvs.open_runs.MAX_AGE_S - 60)
        status, body = self._get(f"/api/members/{MEMBER}/ping")
        self.assertFalse(body["running"])


class Command(Base):
    def test_unknown_member_is_400(self):
        status, body = self._post("/api/members/nonexistent-ghost/command", {"text": "hi"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_empty_text_is_rejected(self):
        status, body = self._post(f"/api/members/{MEMBER}/command", {"text": "   "})
        self.assertEqual(status, 400)

    def test_oversized_text_is_rejected(self):
        status, body = self._post(f"/api/members/{MEMBER}/command", {"text": "x" * 8001})
        self.assertEqual(status, 400)

    def test_command_writes_a_message_and_wakes_the_member(self):
        status, body = self._post(f"/api/members/{MEMBER}/command",
                                  {"text": "check the Q3 backlog", "from": "reif"})
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        self.assertIn("msg_id", body)
        self.assertEqual(body["woke"], True)
        self.assertEqual(len(self.spawned), 1, "wake() should have launched marie's next pass")
        cmd, kw = self.spawned[0]
        self.assertIn(MEMBER, cmd)

        # It's a real open message, addressed to the member, sent by "reif".
        status, msg = self._get(f"/api/messages/{body['msg_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(msg["state"], "open")
        self.assertIsNone(msg["ack_text"])

    def test_unknown_message_id_is_404(self):
        status, body = self._get("/api/messages/999999")
        self.assertEqual(status, 404)


class UnderstoodThenDone(Base):
    """The state machine a `command` message goes through: open -> understood -> done. Driven
    directly through fleet_msg (the member's own pass calls these, not the HTTP layer)."""

    def setUp(self):
        super().setUp()
        status, body = self._post(f"/api/members/{MEMBER}/command", {"text": "ship the report"})
        self.msg_id = body["msg_id"]
        self.conn = self.fvs.fleet_db.connect()

    def test_ack_of_a_command_goes_to_understood_not_acked(self):
        self.fvs.fleet_msg.close(self.conn, MEMBER, self.msg_id,
                                  "Understood: will ship the report", acted=True)
        status, msg = self._get(f"/api/messages/{self.msg_id}")
        self.assertEqual(msg["state"], "understood")
        self.assertTrue(msg["ack_text"].startswith("Understood"))
        self.assertIsNone(msg["done_text"])

    def test_done_before_understood_is_rejected(self):
        with self.assertRaises(ValueError):
            self.fvs.fleet_msg.mark_done(self.conn, MEMBER, self.msg_id, "PR #4021")

    def test_done_after_understood_closes_it(self):
        self.fvs.fleet_msg.close(self.conn, MEMBER, self.msg_id, "Understood: on it", acted=True)
        self.fvs.fleet_msg.mark_done(self.conn, MEMBER, self.msg_id, "PR https://x/pull/4021")
        status, msg = self._get(f"/api/messages/{self.msg_id}")
        self.assertEqual(msg["state"], "done")
        self.assertEqual(msg["done_text"], "PR https://x/pull/4021")
        self.assertIsNotNone(msg["done_at"])

    def test_understood_but_not_done_still_escalates(self):
        self.fvs.fleet_msg.close(self.conn, MEMBER, self.msg_id, "Understood: on it", acted=True)
        far_future = time.time() + 100 * 3600  # comfortably past 2 cadences for any member
        out = self.fvs.fleet_msg.watchdog(self.conn, now=far_future)
        self.assertIn(self.msg_id, out["escalated_to_jefe"],
                      "an acked-but-not-done command must still escalate, same as an unacked one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
