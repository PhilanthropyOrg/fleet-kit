"""A person on FLEET_OPERATOR_EMAILS signs in with an emailed one-time link.

RED before this change: /api/login_email did not exist, so a new operator could only get in by
being handed a key.
"""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
ENV = TMP / "fleet.env"
BASE_ENV = ("FLEET_API_KEY=owner-key\n"
            "FLEET_OPERATOR_EMAILS=Brandon@thegoodproject.net, not-an-email\n"
            "FLEET_VIEW_PUBLIC_URL=https://dino.example.org/fleet/philanthropy\n")
ENV.write_text(BASE_ENV)

MAILS = []


class FakeResend(BaseHTTPRequestHandler):
    def do_POST(self):
        MAILS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"id":"x"}')

    def log_message(self, *a):
        pass


resend = ThreadingHTTPServer(("127.0.0.1", 0), FakeResend)
threading.Thread(target=resend.serve_forever, daemon=True).start()
os.environ.update(FLEET_ENV_FILE=str(ENV), FLEET_LOG_DIR=str(TMP), FLEET_REPO=str(TMP),
                  RESEND_API_URL_BASE=f"http://127.0.0.1:{resend.server_address[1]}",
                  RESEND_API_KEY="re_test", FLEET_ALERT_ENV=str(TMP / "none.env"))
for k in ("FLEET_API_KEY", "FLEET_OPERATOR_EMAILS", "FLEET_VIEW_PUBLIC_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(KIT / "scripts"))
import fleet_view_server as fvs  # noqa: E402
import signin_email  # noqa: E402


class FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def who(cookie):
    h = fvs.Handler.__new__(fvs.Handler)
    h.client_address = ("10.0.0.5", 5555)
    h.headers = FakeHeaders({"Cookie": f"fleet_session={cookie}"})
    return h._authorized()


class EmailSignIn(unittest.TestCase):
    def setUp(self):
        MAILS.clear()
        signin_email._LAST_SENT.clear()
        ENV.write_text(BASE_ENV)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), fvs.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, path, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=30)
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"), r.getheader("Set-Cookie") or "")
        c.close()
        return out

    def token(self):
        link = re.search(r"https://\S+", MAILS[-1]["text"]).group(0)
        return link, link.split("signin=", 1)[1]

    def test_allowed_person_gets_a_link_to_the_configured_address_and_signs_in(self):
        status, body, _ = self.post("/api/login_email", {"email": " brandon@TheGoodProject.net "},
                                    {"Host": "evil.example", "Referer": "https://evil.example/chat"})
        self.assertEqual(status, 200, body)
        self.assertEqual(MAILS[-1]["to"], ["brandon@thegoodproject.net"])
        link, token = self.token()
        # The link's base comes only from fleet.env, never from the request.
        self.assertTrue(link.startswith("https://dino.example.org/fleet/philanthropy/chat?signin="), link)
        status, body, cookie = self.post("/api/login_email_verify", {"token": token})
        self.assertEqual((status, body.get("who")), (200, "brandon"))
        session = re.search(r"fleet_session=([^;]+)", cookie).group(1)
        self.assertEqual(who(session), "brandon")
        # Single use.
        status, _, _ = self.post("/api/login_email_verify", {"token": token})
        self.assertEqual(status, 401)

    def test_unlisted_email_gets_the_same_answer_and_no_mail(self):
        status, body, _ = self.post("/api/login_email", {"email": "stranger@example.org"})
        self.assertEqual(status, 200, body)
        self.assertEqual(MAILS, [])

    def test_removing_the_email_cuts_off_the_session(self):
        self.post("/api/login_email", {"email": "brandon@thegoodproject.net"})
        _, token = self.token()
        _, _, cookie = self.post("/api/login_email_verify", {"token": token})
        session = re.search(r"fleet_session=([^;]+)", cookie).group(1)
        # Stale copy the entrypoint loaded at start must not keep the removed email in.
        os.environ["FLEET_OPERATOR_EMAILS"] = "brandon@thegoodproject.net"
        ENV.write_text("FLEET_API_KEY=owner-key\nFLEET_VIEW_PUBLIC_URL=https://dino.example.org/x\n")
        try:
            self.assertEqual(who(session), "")
        finally:
            os.environ.pop("FLEET_OPERATOR_EMAILS", None)

    def test_forged_or_expired_sessions_and_tokens_fail(self):
        self.assertEqual(who("e:brandon@thegoodproject.net:" + "0" * 64), "")
        self.post("/api/login_email", {"email": "brandon@thegoodproject.net"})
        _, token = self.token()
        import time
        self.assertEqual(signin_email.redeem(token, fvs.read_env_values(), now=time.time() + 16 * 60), "")

    def test_asking_again_within_a_minute_sends_nothing(self):
        self.post("/api/login_email", {"email": "brandon@thegoodproject.net"})
        self.post("/api/login_email", {"email": "brandon@thegoodproject.net"})
        self.assertEqual(len(MAILS), 1)

    def test_off_without_a_public_url(self):
        ENV.write_text("FLEET_OPERATOR_EMAILS=brandon@thegoodproject.net\n")
        status, body, _ = self.post("/api/login_email", {"email": "brandon@thegoodproject.net"})
        self.assertEqual(status, 503, body)
        self.assertIn("FLEET_VIEW_PUBLIC_URL", body["error"])
        self.assertEqual(MAILS, [])


if __name__ == "__main__":
    unittest.main()
