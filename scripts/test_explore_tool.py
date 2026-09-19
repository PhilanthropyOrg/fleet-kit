"""Reif 2026-09-19: "it's fully scripted right? I want to give it an LLM and a job to do --
and see what it does. Ie like a real user."

explore.py is the unscripted half of sentry's pass: a persistent browser with verbs the model
chooses between, against a local page here so the test needs no network."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPLORE = ROOT / "scripts" / "explore.py"

PAGE = b"""<!doctype html><title>Shop</title><body>
<h1>Find a nonprofit</h1>
<a href="/next">Open the report</a>
<button onclick="document.title='clicked'">Claim this org</button>
<script>console.error('boom: a real console error');
fetch('/missing').catch(()=>{});</script>
</body>"""
NEXT = b"<!doctype html><title>Report</title><body><h1>Financials</h1><p>Program spend 91%</p></body>"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/next":
            body, code = NEXT, 200
        elif self.path == "/missing":
            body, code = b"nope", 404
        else:
            body, code = PAGE, 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: A003
        pass


class ExploreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def run_cmd(self, *args):
        env = dict(os.environ, EXPLORE_DIR=self.tmp.name)
        p = subprocess.run([sys.executable, str(EXPLORE), *args], capture_output=True, text=True, timeout=180, env=env)
        return p.returncode, p.stdout + p.stderr

    def test_open_reports_page_text_and_console_errors(self):
        rc, out = self.run_cmd("open", f"http://127.0.0.1:{self.port}/")
        self.assertEqual(rc, 0, out)
        self.assertIn("Find a nonprofit", out)
        self.assertIn("PROBLEMS SEEN", out)
        self.assertIn("boom: a real console error", out)

    def test_links_then_click_navigates(self):
        self.run_cmd("open", f"http://127.0.0.1:{self.port}/")
        rc, out = self.run_cmd("links")
        self.assertEqual(rc, 0, out)
        self.assertIn("Open the report", out)
        self.assertIn("Claim this org", out)
        rc, out = self.run_cmd("click", "Open the report")
        self.assertEqual(rc, 0, out)
        self.assertIn("/next", out)
        self.assertIn("Financials", out)

    def test_shot_writes_a_png(self):
        self.run_cmd("open", f"http://127.0.0.1:{self.port}/")
        rc, out = self.run_cmd("shot")
        self.assertEqual(rc, 0, out)
        path = next(l.split("screenshot: ", 1)[1].strip() for l in out.splitlines() if "screenshot:" in l)
        self.assertTrue(Path(path).exists() and Path(path).stat().st_size > 0, path)

    def test_charter_tells_sentry_to_explore(self):
        md = (ROOT / "members" / "sentry" / "sentry.md").read_text()
        self.assertIn("explore.py", md)
        self.assertIn("go be a person", md)
        spec = json.loads((ROOT / "members" / "sentry" / "sentry.fleet.json").read_text())
        self.assertIn("Bash", spec["llm"]["tools"]["allow"])


if __name__ == "__main__":
    unittest.main()
