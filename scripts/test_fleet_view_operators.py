"""One key per person on the dashboard, and the chat page that asks Claude about the fleet.

RED before this change:
  - only FLEET_API_KEY could sign in, so a second operator had to share it, and a write was
    never recorded with a name (writes were not logged at all);
  - /api/chat did not exist.
"""
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
ENV = TMP / "fleet.env"
ENV.write_text("FLEET_API_KEY=owner-key\nFLEET_API_KEY_OWNER=reif\n"
               "FLEET_OPERATOR_KEYS=brandon:brandon-key, broken, :nokey\n"
               "FLEET_ACCOUNTS=primary\n")
os.environ.update(FLEET_ENV_FILE=str(ENV), FLEET_LOG_DIR=str(TMP), FLEET_REPO=str(TMP))
os.environ.pop("FLEET_API_KEY", None)
os.environ.pop("FLEET_OPERATOR_KEYS", None)
sys.path.insert(0, str(KIT / "scripts"))
import fleet_view_server as fvs  # noqa: E402


class FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def who(client, headers):
    h = fvs.Handler.__new__(fvs.Handler)
    h.client_address = (client, 5555)
    h.headers = FakeHeaders(headers)
    return h._authorized()


class Server:
    def __enter__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), fvs.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def post(self, path, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=30)
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}"), r.getheader("Set-Cookie") or ""

    def __exit__(self, *a):
        self.srv.shutdown()


class EachPersonHasTheirOwnKey(unittest.TestCase):
    def test_keys_parse_and_skip_malformed_entries(self):
        self.assertEqual(fvs.operator_keys(), [("reif", "owner-key"), ("brandon", "brandon-key")])

    def test_remote_caller_is_named_by_the_key_it_sent(self):
        self.assertEqual(who("10.0.0.5", {"X-Fleet-Key": "brandon-key"}), "brandon")
        self.assertEqual(who("10.0.0.5", {"X-Fleet-Key": "owner-key"}), "reif")
        self.assertEqual(who("10.0.0.5", {"X-Fleet-Key": "wrong"}), "")
        self.assertEqual(who("10.0.0.5", {}), "")

    def test_session_cookie_names_the_person(self):
        cookie = f"fleet_session={fvs._session_token('brandon-key')}"
        self.assertEqual(who("10.0.0.5", {"Cookie": cookie}), "brandon")
        # The owner's cookie is unchanged, so nobody already signed in is logged out.
        cookie = f"fleet_session={fvs._session_token('owner-key')}"
        self.assertEqual(who("10.0.0.5", {"Cookie": cookie}), "reif")

    def test_removing_a_persons_key_cuts_only_them_off(self):
        # The container's entrypoint also loads fleet.env into the server's environment at
        # start; that stale copy must not keep a removed key working (live 2026-09-30).
        os.environ["FLEET_OPERATOR_KEYS"] = "brandon:brandon-key"
        ENV.write_text("FLEET_API_KEY=owner-key\nFLEET_API_KEY_OWNER=reif\n")
        try:
            self.assertEqual(who("10.0.0.5", {"X-Fleet-Key": "brandon-key"}), "")
            self.assertEqual(who("10.0.0.5", {"X-Fleet-Key": "owner-key"}), "reif")
        finally:
            os.environ.pop("FLEET_OPERATOR_KEYS", None)
            ENV.write_text("FLEET_API_KEY=owner-key\nFLEET_API_KEY_OWNER=reif\n"
                           "FLEET_OPERATOR_KEYS=brandon:brandon-key, broken, :nokey\n"
                           "FLEET_ACCOUNTS=primary\n")

    def test_on_behalf_header_counts_only_from_the_box(self):
        self.assertEqual(who("127.0.0.1", {"X-Fleet-On-Behalf": "brandon"}), "brandon (chat)")
        self.assertEqual(who("10.0.0.5", {"X-Fleet-On-Behalf": "brandon"}), "")

    def test_login_with_a_persons_key_sets_their_cookie(self):
        with Server() as s:
            status, body, cookie = s.post("/api/login", {"key": "brandon-key"})
            self.assertEqual((status, body.get("who")), (200, "brandon"))
            self.assertIn(fvs._session_token("brandon-key"), cookie)
            status, _, _ = s.post("/api/login", {"key": "nope"})
            self.assertEqual(status, 401)

    def test_a_write_is_logged_with_the_persons_name(self):
        log = TMP / "writes.jsonl"
        log.unlink(missing_ok=True)
        with Server() as s:
            s.post("/api/members/no-such-member/pause", {}, {"X-Fleet-On-Behalf": "brandon"})
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([(r["path"], r["who"]) for r in rows],
                         [("/api/members/no-such-member/pause", "brandon (chat)")])


class ChatAsksClaudeThroughTheAccountPool(unittest.TestCase):
    def setUp(self):
        # A stand-in `claude` that records how it was called and answers.
        self.bin = TMP / "bin"
        self.bin.mkdir(exist_ok=True)
        fake = self.bin / "claude"
        fake.write_text("#!/bin/sh\n"
                        f"printf '%s\\n' \"$@\" > {TMP}/claude-args\n"
                        f"env > {TMP}/claude-env\n"
                        "echo '{\"type\":\"result\",\"result\":\"nerd files issues\"}'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.path}"
        os.environ["FLEET_API_KEY"] = "must-not-leak"

    def tearDown(self):
        os.environ["PATH"] = self.path
        os.environ.pop("FLEET_API_KEY", None)

    def test_question_gets_an_answer_only_the_asker_can_read(self):
        with Server() as s:
            status, body, _ = s.post("/api/chat", {"question": "what does nerd do?"},
                                     {"X-Fleet-On-Behalf": "brandon"})
            self.assertEqual(status, 202, body)
            job = body["id"]
            for _ in range(100):
                _, polled, _ = s.post("/api/chat/poll", {"id": job}, {"X-Fleet-On-Behalf": "brandon"})
                if polled.get("status") != "running":
                    break
                time.sleep(0.1)
            self.assertEqual((polled["status"], polled["answer"]), ("done", "nerd files issues"), polled)
            status, _, _ = s.post("/api/chat/poll", {"id": job}, {"X-Fleet-On-Behalf": "someone-else"})
            self.assertEqual(status, 404)

        args = (TMP / "claude-args").read_text().splitlines()
        self.assertIn("--restricted", args)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob,Bash")
        self.assertTrue(args[args.index("--allowedTools") + 1].startswith("Bash(python3 "))
        self.assertIn("Read(**/fleet.env*)", args[args.index("--settings") + 1])
        env = (TMP / "claude-env").read_text()
        self.assertNotIn("must-not-leak", env)
        self.assertNotIn("brandon-key", env)
        self.assertIn("FLEET_CHAT_WHO=brandon (chat)", env)
        rows = [json.loads(line) for line in (TMP / "chat.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["who"], "brandon (chat)")

    def test_empty_question_is_refused(self):
        with Server() as s:
            status, body, _ = s.post("/api/chat", {"question": "  "}, {"X-Fleet-On-Behalf": "brandon"})
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
