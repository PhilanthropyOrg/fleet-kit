#!/usr/bin/env python3
"""The run card's two jobs: hand the reader the run, and grade the pass honestly.

Reif, 2026-09-18, on a judge-judy card reading `SELF-CRITIQUE: none -- clean single-pass
verdict` with an unselectable run id:

    "this should be copyable, and internally referenced"
    "no such thing as a 'clean' run, something needs to improve each run"
    "if you are critiquing and not making changes, then you are a soap opera, lots of words
     said, no change"

Three behaviours, driven through the real page in a real browser because all three are pixels:

  1. The run id is a copy button, and what it copies is the id PLUS a deep link -- "internally
     referenced" means a run id pasted into a chat resolves back to the pass quickly, and an id
     with no link makes the reader hunt for the page it belongs to.
  2. `#run=<id>` on a cold load opens that run's panel -- the other half of the same job.
  3. The self-critique is graded on what it can point at. A referenced improvement renders as
     an improvement; words with no issue/PR/file render as "said, not done"; empty or
     `none`-shaped renders as "improved nothing". Both failures are visible, neither is tidy.

Run: python3 scripts/test_run_card_reference.py
"""
from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE_HTML = (HERE / "fleet_home.html").read_bytes()

ok, fail = [], []

# Three runs, one per critique state. judge-judy's is the real card from the screenshot.
NOW = time.time()
RUNS = [
    {"run_id": "review-6715-98c41f3a49f1", "member": "judge-judy", "status": "ok",
     "ts": NOW - 540, "kind": "review", "pr": 6715,
     "outcome": "approved PR #6715",
     "evidence": "head 98c41f3a49f1, fleet-code-review: success",
     "report": "approved -- no findings",
     "self_critique": "none -- clean single-pass verdict",
     "tokens": {"cost_usd": 0.48, "num_turns": 13, "duration_ms": 60000}},
    {"run_id": "sweep-4102-aabbccdd", "member": "marie", "status": "ok",
     "ts": NOW - 900, "kind": "sweep",
     "outcome": "riced 12 items",
     "self_critique": "I should have written down why I skipped the stale ones",
     "tokens": {"cost_usd": 0.12, "num_turns": 6, "duration_ms": 120000}},
    {"run_id": "fix-9001-ddeeff00", "member": "the-fixer", "status": "ok",
     "ts": NOW - 1200, "kind": "fix",
     "outcome": "reopened the red lane",
     "self_critique": "my skip reason never reached the record, so I filed #6720 to capture it",
     "tokens": {"cost_usd": 0.31, "num_turns": 9, "duration_ms": 180000}},
]


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE_HTML)))
            self.end_headers()
            self.wfile.write(PAGE_HTML)
            return
        if urlparse(self.path).path == "/favicon.ico":
            # The page declares no favicon, so Chromium asks for one and logs a console error
            # when it 404s. That is the harness's noise, not the page's -- answer it empty so
            # the zero-console-errors check stays a real signal.
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def _mocks(route):
    path = urlparse(route.request.url).path
    if path == "/api/snapshot":
        route.fulfill(json={"runs": RUNS, "gh": {}})
    elif path == "/api/number":
        route.fulfill(json={"configured": False})
    elif path == "/api/fleet_state":
        route.fulfill(json={"FLEET_ENABLED": True, "BRAND": "Fleet"})
    elif path in ("/api/plan", "/api/asks", "/api/members", "/api/kpi", "/api/spend",
                  "/api/build", "/api/metrics", "/api/budget_preview", "/api/minion_runs"):
        route.fulfill(json={"members": [], "asks": [], "kpi": [], "spend": [], "metrics": [], "runs": []})
    elif path == "/api/iph":
        route.fulfill(json={"series": {"hours": [], "direct": [], "mega_child": [],
                                        "closed_without_pr": [], "avg_24h": [], "avg_7d": [],
                                        "current_24h_rate": 0}, "r24": 0, "svg": "", "unavailable": True})
    else:
        # Every /api/* route this page touches is mocked above; anything else is the test's
        # own gap, so fail loudly here rather than letting a 404 masquerade as a page error.
        raise AssertionError("unmocked route: " + path)


def _open(page, port, run_id):
    """Cold-load the page straight onto one run's panel via the deep link."""
    page.goto(f"http://127.0.0.1:{port}/#run={run_id}", timeout=15000)
    page.wait_for_selector("#side:not([hidden])", timeout=8000)
    return page.text_content("#side")


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(viewport={"width": 420, "height": 900},
                                      permissions=["clipboard-read", "clipboard-write"])
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/api/**", _mocks)

            # --- 2. deep link opens the panel (this is also how every check below loads) ---
            text = _open(page, port, "review-6715-98c41f3a49f1")

            def _deep_link_opens_the_run():
                assert "judge-judy" in text, text[:300]
                assert "review-6715-98c41f3a49f1" in text, text[:300]
            check("A pasted #run=<id> link lands on that run's panel", _deep_link_opens_the_run)

            def _no_console_errors():
                assert not errors, f"console/page errors: {errors}"
            check("The run panel renders with zero console/page errors", _no_console_errors)

            # --- 1. the run id copies itself AND its link ---
            def _run_id_is_a_copy_button():
                btn = page.query_selector("#side .runid")
                assert btn, "run id is not a button -- nothing to click to copy"
                assert "review-6715-98c41f3a49f1" in (btn.text_content() or "")
            check("The run id renders as a copy button", _run_id_is_a_copy_button)

            def _copy_carries_id_and_link():
                page.click("#side .runid")
                page.wait_for_timeout(300)
                got = page.evaluate("navigator.clipboard.readText()")
                assert "review-6715-98c41f3a49f1" in got, f"id missing from clipboard: {got!r}"
                assert "#run=review-6715-98c41f3a49f1" in got, \
                    f"deep link missing from clipboard -- a bare id makes the reader hunt: {got!r}"
            check("Clicking it copies the id AND a link that resolves back to the run",
                  _copy_carries_id_and_link)

            # --- 3. the critique is graded on what it points at ---
            def _none_reads_as_improved_nothing():
                assert "improved nothing" in text.lower(), \
                    f"'none -- clean single-pass verdict' still reads as clean: {text!r}"
                assert "clean single-pass verdict" not in text, \
                    "the none-shaped critique is still rendered as a fact"
            check("A `none`-shaped critique reads as 'improved nothing', not a clean run",
                  _none_reads_as_improved_nothing)

            def _words_without_a_reference_read_as_said_not_done():
                t = _open(page, port, "sweep-4102-aabbccdd")
                assert "Said, not done" in t, f"unreferenced critique rendered as fine: {t!r}"
                assert "should have written down" in t, "the critique text itself was dropped"
            check("A critique with no issue/PR/file behind it reads as 'said, not done'",
                  _words_without_a_reference_read_as_said_not_done)

            def _a_referenced_improvement_reads_as_an_improvement():
                t = _open(page, port, "fix-9001-ddeeff00")
                assert "#6720" in t, f"the filed item is missing: {t!r}"
                assert "Said, not done" not in t, "a real, referenced improvement was flagged as words"
                assert "improved nothing" not in t.lower()
            check("A critique that points at a filed item reads as a real improvement",
                  _a_referenced_improvement_reads_as_an_improvement)

            shot = Path("/tmp/run-card-critique.png")
            _open(page, port, "review-6715-98c41f3a49f1")
            page.screenshot(path=str(shot))
            print(f"screenshot: {shot}")
            browser.close()
    finally:
        server.shutdown()

    for n in ok:
        print(f"  PASS  {n}")
    for n, why in fail:
        print(f"  FAIL  {n}\n        {why}")
    print(f"\n{len(ok)} passed, {len(fail)} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
