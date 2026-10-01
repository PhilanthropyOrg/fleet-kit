"""/about explains the fleet to someone who has never seen it (Reif 2026-09-30: "someone coming
off the street can see the fleet page and understand what it does").

RED before this change: /about did not exist and Home had no way to reach it.
"""
import http.client
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
(TMP / "fleet.env").write_text("FLEET_API_KEY=k\n")
os.environ.update(FLEET_ENV_FILE=str(TMP / "fleet.env"), FLEET_LOG_DIR=str(TMP), FLEET_REPO=str(TMP))
sys.path.insert(0, str(KIT / "scripts"))
import fleet_view_server as fvs  # noqa: E402


class AboutPage(unittest.TestCase):
    def test_about_is_served_to_a_stranger_without_signing_in(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), fvs.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
            c.request("GET", "/about", headers={"X-Forwarded-For": "203.0.113.9"})
            r = c.getresponse()
            body = r.read().decode()
        finally:
            srv.shutdown()
        self.assertEqual(r.status, 200)
        self.assertIn("A team of AI workers", body)
        for api in ("api/fleet_state", "api/members", "api/metrics"):   # live, not hardcoded
            self.assertIn(api, body)

    def test_home_links_to_it(self):
        home = (KIT / "scripts" / "fleet_home.html").read_text()
        self.assertIn('id="navAbout"', home)
        self.assertIn("withBase('/about')", home)


if __name__ == "__main__":
    unittest.main()
