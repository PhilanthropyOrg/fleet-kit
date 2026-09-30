"""philanthropy#9271: search-and-open-org must pass when a page is on screen but its `load`
event is late.

Measured from dino (the sentry box) 2026-09-30: Cloudflare injects its bot-check scripts into
every philanthropy.org page, and one of them (brunhild.challenges.cloudflare.com) is
ERR_ADDRESS_UNREACHABLE from there, so `load` fires 8-30s after the HTML is on screen. The
walker waited for `load` (Playwright's default), so "Click the first result row" timed out with
the Kaiser report already rendered in its own screenshot, and step 0's goto hit its 30s limit.

The fixture here holds each page's `load` with one slow image, the same shape as that script.
"""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_walker as jw  # noqa: E402

CATALOG_PATH = Path(__file__).resolve().parent.parent / "members" / "sentry" / "journeys.yaml"
SLOW_S = 12  # longer than the 5s patched goto limit and the 10s click-through wait, so waiting on `load` must fail


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _SlowLoadHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow.png"):
            time.sleep(SLOW_S)
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/990/report/"):
            body = '<h1>Kaiser Foundation Hospitals</h1><img src="/slow.png?r">'
        else:
            body = ('<table><tr><td><a href="/990/report/941105628">Kaiser Foundation Hospitals</a>'
                    '</td><td>$3.4B</td></tr></table><img src="/slow.png?s">')
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())

    def log_message(self, *a):
        pass


class SearchClickThroughLateLoadEventTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), _SlowLoadHandler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_late_load_event_still_passes(self):
        catalog = jw.load_catalog(CATALOG_PATH)
        search = next(j for j in catalog["journeys"] if j["id"] == "search-and-open-org")
        search["viewports"] = ["desktop"]
        users = jw.TestUsers(env={"PHILANTHROPY_BASE_URL": f"http://127.0.0.1:{self.port}",
                                  "ATLAS_TEST_BYPASS": "test-bypass-token"})
        old = jw.NAV_TIMEOUT_MS
        jw.NAV_TIMEOUT_MS = 5000  # SLOW_S outlasts it, as the 26-30s real `load` outlasts 30s
        try:
            journeys_out, blocked = jw.run_all(catalog, users, self.browser, Path("/tmp"),
                                                "search-late-load-run",
                                                journey_filter=["search-and-open-org"])
        finally:
            jw.NAV_TIMEOUT_MS = old

        self.assertEqual(blocked, [])
        steps = journeys_out[0]["steps"]
        self.assertEqual([s["status"] for s in steps], ["pass", "pass"], steps)


if __name__ == "__main__":
    unittest.main()
