"""The home console signs you in where you are.

Reif, 2026-09-19, on the fleet console: pressed Rerun now, got "sign in first (Settings on
the classic console)" -- "thats stupid". A write button on THIS page sent the operator to a
different page to unlock it. Now a 401 opens a key box inline, POSTs the same /api/login the
classic console uses, and retries the click once the cookie is set.

Drives the real page in headless Chromium against a stub API: /api/run_now answers 401 until
/api/login has seen the right key. Passes only if the button ends up reading "Started".

Run: python3 scripts/test_fleet_home_inline_signin.py
"""
from __future__ import annotations

import http.server
import threading
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE_HTML = (HERE / "fleet_home.html").read_bytes()
KEY = "test-key-123"
STATE = {"logged_in": False, "run_now": 0}
ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE_HTML)))
        self.end_headers()
        self.wfile.write(PAGE_HTML)

    def log_message(self, *a):
        pass


MEMBER = {"spec": {"name": "librarian", "emoji": "", "plain": "keeps memory small"},
          "effective": {"enabled": True}}


def _mocks(route):
    path = urlparse(route.request.url).path
    if route.request.method == "POST":
        body = route.request.post_data_json or {}
        if path == "/api/login":
            if body.get("key") == KEY:
                STATE["logged_in"] = True
                route.fulfill(json={"ok": True})
            else:
                route.fulfill(status=401, json={"ok": False, "error": "wrong key"})
        elif path == "/api/run_now":
            if STATE["logged_in"]:
                STATE["run_now"] += 1
                route.fulfill(json={"ok": True})
            else:
                route.fulfill(status=401, json={"ok": False, "error": "unauthorized -- sign in on the Settings page"})
        else:
            route.fulfill(status=404, json={"error": "unmocked POST " + path})
        return
    if path == "/api/snapshot":
        route.fulfill(json={"runs": [], "gh": {"merged": [], "needs_human_op": {"count": 0, "oldest_age_hours": 0}}})
    elif path == "/api/members":
        route.fulfill(json={"members": [MEMBER]})
    elif path == "/api/kpi":
        route.fulfill(json={"kpi": []})
    elif path == "/api/build":
        route.fulfill(json={})
    elif path == "/api/fleet_state":
        route.fulfill(json={"FLEET_ENABLED": True, "BRAND": "Fleet Kit"})
    elif path == "/api/metrics":
        route.fulfill(json={"metrics": []})
    else:
        route.fulfill(json={})


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/api/**", _mocks)
            page.goto(f"http://127.0.0.1:{port}/", timeout=15000)
            page.wait_for_selector("tr.agent[data-agent='librarian']", timeout=10000)
            page.click("tr.agent[data-agent='librarian']")
            page.click("button[data-act='rerun']")
            page.wait_for_timeout(500)

            def _key_box_opens_here():
                assert page.is_visible("#signin"), "no inline sign-in box after a 401"
                err = page.text_content("tr.drawer [data-act='err']") or ""
                assert "classic" not in err.lower(), f"still sent to the classic console: {err!r}"
            check("A 401 on Rerun opens a key box on this page, never a trip to the classic console", _key_box_opens_here)

            if not page.is_visible("#signin"):
                browser.close(); server.shutdown()
                for name, err in fail: print(f"FAIL {name}\n     {err}")
                return 1
            page.fill("#signinKey", "wrong")
            page.click("#signinGo")
            page.wait_for_timeout(300)

            def _wrong_key_says_so():
                assert page.is_visible("#signin")
                assert "wrong key" in (page.text_content("#signinMsg") or "")
                assert page.input_value("#signinKey") == "", "the key stayed in the input"
            check("A wrong key is refused in place, and the input is cleared either way", _wrong_key_says_so)

            page.fill("#signinKey", KEY)
            page.press("#signinKey", "Enter")
            page.wait_for_timeout(600)

            def _right_key_retries_the_click():
                assert not page.is_visible("#signin"), "box still open after a good key"
                assert STATE["run_now"] == 1, f"run_now calls after sign-in: {STATE['run_now']}"
                assert page.text_content("button[data-act='rerun']").strip() == "Started"
            check("The right key signs in and the original Rerun goes through, button reads Started", _right_key_retries_the_click)

            def _no_page_errors():
                assert not errors, errors
            check("No page errors along the way", _no_page_errors)
            browser.close()
    finally:
        server.shutdown()
    for name in ok:
        print(f"ok   {name}")
    for name, err in fail:
        print(f"FAIL {name}\n     {err}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
