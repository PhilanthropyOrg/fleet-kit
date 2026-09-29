#!/usr/bin/env python3
"""test_staff_journeys_7988.py -- philanthropy#7988: sentry walks Reif's own staff jobs.

The three Given/When/Then lines, end to end through the real walker (run_all) and the real
filer (journey_issue_filer.process), with only the browser, the QA session endpoint, Resend
and `gh` faked:

  * a staff step answering 500, or taking 3.5s on the server, files ONE issue carrying the URL,
    the server time and a screenshot; the next run comments on it instead of filing a twin;
  * a Resend send log with the same subject twice to one person files one `incident` issue;
  * a clean run files nothing.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_walker as jw  # noqa: E402
import journey_issue_filer as jif  # noqa: E402
import journey_hq  # noqa: E402

CATALOG = jw.load_catalog(jw.ROOT / "members" / "sentry" / "journeys.yaml")
STAFF = "staff-daily-jobs"
RESEND = "resend-no-duplicate-sends"


# --- fakes ------------------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status: int, server_ms: int):
        self.status = status
        self.request = type("Req", (), {"timing": {"requestStart": 10.0, "responseStart": 10.0 + server_ms}})()


class FakeLocator:
    def __init__(self, page, sel: str):
        self.page, self.sel = page, sel
        self.first = self

    def wait_for(self, **_):
        return None

    def count(self) -> int:
        return 1

    def nth(self, _i):
        return self

    def locator(self, sel):
        return FakeLocator(self.page, sel)

    def press(self, key):
        self.page.pressed.append((self.sel, key))

    def click(self, **_):  # a staff journey must never click Approve/Reject on prod
        self.page.clicked.append(self.sel)

    def get_attribute(self, name):
        return {"data-tid": "42", "aria-expanded": "true"}.get(name)

    def is_visible(self):
        return True


class FakePage:
    def __init__(self, answers: dict):
        self.answers = answers  # path -> (status, server_ms)
        self.url = "about:blank"
        self.visited, self.pressed, self.clicked = [], [], []

    def on(self, *_):
        pass

    def goto(self, url, **_):
        self.url = url
        self.visited.append(url)
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
        status, ms = next((v for k, v in self.answers.items() if path.rstrip("/").endswith(k.rstrip("/"))), (200, 150))
        return FakeResp(status, ms)

    def locator(self, sel):
        return FakeLocator(self, sel)

    def wait_for_function(self, *_, **__):
        return None

    def wait_for_load_state(self, *_, **__):
        return None

    def evaluate(self, *_):
        return 390

    def screenshot(self, path):
        Path(path).write_bytes(b"png")


class FakeBrowser:
    def __init__(self, answers):
        self.answers = answers
        self.pages = []

    def new_context(self, **_):
        browser = self

        class Ctx:
            def route(self, *_):
                pass

            def new_page(self):
                p = FakePage(browser.answers)
                browser.pages.append(p)
                return p

            def close(self):
                pass
        return Ctx()


class QaEndpoint:
    """POST /990/api/qa/{reset,session}: the session answer is a magic link."""

    def __call__(self, req, timeout=None):
        body = {"url": "https://philanthropy.org/auth/magic?t=x"} if req.full_url.endswith("/session") else {}

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(body).encode()
        return R()


class LabelledGh:
    """In-memory `gh issue`: create/list/comment/close, remembering labels."""

    def __init__(self):
        self.issues, self._next = {}, 100

    def __call__(self, cmd):
        if cmd[1] == "label":
            return 0, "ok"
        sub = cmd[2]
        if sub == "create":
            n, self._next = self._next, self._next + 1
            labels = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
            self.issues[n] = {"title": cmd[cmd.index("--title") + 1], "body": cmd[cmd.index("--body") + 1],
                              "labels": labels, "open": True, "comments": []}
            return 0, f"https://github.com/x/y/issues/{n}"
        if sub == "list":
            return 0, json.dumps([{"number": n, "title": v["title"], "body": v["body"]}
                                  for n, v in self.issues.items() if v["open"]])
        if sub == "comment":
            self.issues[int(cmd[3])]["comments"].append(cmd[cmd.index("--body") + 1])
            return 0, "ok"
        if sub == "close":
            self.issues[int(cmd[3])]["open"] = False
            return 0, "ok"
        raise AssertionError(cmd)


# --- harness ----------------------------------------------------------------------------------

class StaffWalkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.users = jw.TestUsers({"QA_SESSION_TOKEN": "t", "PHILANTHROPY_BASE_URL": "https://philanthropy.org"})
        self.gh = LabelledGh()
        self.state = self.tmp / "state.json"

    def walk(self, run: str, answers=None, sends=None, only=(STAFF,)):
        browser = FakeBrowser(answers or {})
        with mock.patch("urllib.request.urlopen", QaEndpoint()), \
                mock.patch.object(journey_hq, "resend_key", lambda: "re_test"), \
                mock.patch.object(journey_hq, "resend_last_24h", lambda _k: list(sends or [])):
            journeys, blocked = jw.run_all(CATALOG, self.users, browser, self.tmp, run, list(only))
        self.assertEqual(blocked, [])
        self.browser = browser
        path = self.tmp / f"{run}.json"
        path.write_text(json.dumps({"run": run, "deploy_sha": "abc", "journeys": journeys}))
        summary = jif.process(path, self.state, runner=self.gh)
        return journeys, summary

    # Given the staff journeys, When sentry's daily walk runs, Then each step records a
    # pass/fail and time.
    def test_every_staff_step_records_pass_and_server_time_at_both_widths(self):
        journeys, summary = self.walk("r1")
        self.assertEqual([j["id"] for j in journeys], [f"{STAFF}--desktop_1440", f"{STAFF}--mobile_390"])
        for j in journeys:
            self.assertEqual({s["status"] for s in j["steps"]}, {"pass"}, j)
            timed = [s for s in j["steps"] if "server_ms" in s]
            self.assertGreaterEqual(len(timed), 5, "overview, queue, inbox, a conversation and HQ home are each timed")
            self.assertTrue(all(isinstance(s["duration_ms"], int) for s in j["steps"]))
        visited = " ".join(u for p in self.browser.pages for u in p.visited)
        for path in ("/network/hq/ops/status", "/network/hq/ops/approve", "/network/hq/messages",
                     "/network/hq/messages/42", "/network/hq"):
            self.assertIn(path, visited)
        # A claim is opened read-only (row expanded with the keyboard), never decided.
        self.assertTrue(any("vq-row" in sel for p in self.browser.pages for sel, _ in p.pressed))
        self.assertEqual([c for p in self.browser.pages for c in p.clicked], [])
        self.assertEqual(summary["filed"], [], "a clean run files nothing")

    # Given a 5xx on a staff step, When the walk hits it, Then an issue is filed that run.
    def test_a_500_files_one_issue_with_url_timing_and_screenshot_then_dedupes(self):
        _, s1 = self.walk("r1", answers={"/network/hq/messages": (500, 120)})
        self.assertEqual(len(s1["filed"]), 1, s1)
        issue = self.gh.issues[s1["filed"][0]["issue"]]
        self.assertIn("500", issue["body"])
        self.assertIn("https://philanthropy.org/network/hq/messages", issue["body"])
        self.assertIn("Server time", issue["body"])
        self.assertIn("**Screenshot:**", issue["body"])
        self.assertIn("desktop_1440, mobile_390", issue["body"])  # both widths, one issue

        _, s2 = self.walk("r2", answers={"/network/hq/messages": (500, 120)})
        self.assertEqual(s2["filed"], [], "the re-run comments, it does not file a twin")
        self.assertEqual(len(s2["commented"]), 1)
        self.assertEqual(len(self.gh.issues), 1)

    # Given a page slower than 3s server-side, When the walk hits it, Then an issue is filed.
    def test_a_3500ms_server_time_files_one_issue(self):
        _, s = self.walk("r1", answers={"/network/hq/ops/approve": (200, 3500)})
        self.assertEqual(len(s["filed"]), 1, s)
        body = self.gh.issues[s["filed"][0]["issue"]]["body"]
        self.assertIn("3500ms", body)
        self.assertIn("/network/hq/ops/approve", body)
        # slow is still usable: the claim is still opened (read-only) after a slow queue
        self.assertTrue(any("vq-row" in sel for p in self.browser.pages for sel, _ in p.pressed))

    # Given a duplicate email to one recipient in 24h, When the walk checks Resend, Then an
    # incident issue is filed.
    def test_a_duplicate_resend_files_one_incident(self):
        sends = [
            {"to": ["ed@charity.org"], "subject": "Your claim is approved", "created_at": "2026-09-29 10:00:00+00"},
            {"to": ["ed@charity.org"], "subject": "Your claim is approved", "created_at": "2026-09-29 11:00:00+00"},
            {"to": ["amy@charity.org"], "subject": "Your claim is approved", "created_at": "2026-09-29 11:00:00+00"},
        ]
        _, s = self.walk("r1", sends=sends, only=(RESEND,))
        self.assertEqual(len(s["filed"]), 1, s)
        issue = self.gh.issues[s["filed"][0]["issue"]]
        self.assertIn("incident", issue["labels"])
        self.assertIn('2x ed***@charity.org "Your claim is approved"', issue["body"])

        _, s2 = self.walk("r2", sends=sends, only=(RESEND,))
        self.assertEqual(s2["filed"], [])
        self.assertEqual(len(self.gh.issues), 1)

    def test_clean_resend_files_nothing(self):
        sends = [{"to": ["ed@charity.org"], "subject": "Welcome", "created_at": "2026-09-29 10:00:00+00"}]
        _, s = self.walk("r1", sends=sends, only=(RESEND,))
        self.assertEqual(s["filed"], [])
        self.assertEqual(self.gh.issues, {})


class WebSocketBypassGapTest(unittest.TestCase):
    """The bypass header never reaches a WebSocket handshake, so Cloudflare challenges the live
    feed's socket for the walker only: recorded as evidence, never a failed step."""

    WS_403 = ("WebSocket connection to 'wss://philanthropy.org/990/socket.io/?EIO=4&transport=websocket' "
              "failed: Error during WebSocket handshake: Unexpected response code: 403")

    def step_with_console(self, env, text):
        journey = {"id": "j", "steps": [{"action": "a", "observable_result": "o"}]}
        ctx = jw.JourneyCtx(journey, "desktop", {"width": 1, "height": 1}, FakeBrowser({}),
                            jw.TestUsers(env), Path(tempfile.mkdtemp()), "r")
        page = ctx.page()
        ctx._console_errors[id(page)].append(("console", text, None))
        return ctx.step(0, lambda: None, page), ctx.results[0]

    def test_ws_403_with_bypass_is_recorded_not_failed(self):
        ok, result = self.step_with_console({"ATLAS_TEST_BYPASS": "b"}, self.WS_403)
        self.assertTrue(ok, result)
        self.assertIn("403", result["console_errors"][0])

    def test_ws_403_without_bypass_still_fails(self):
        ok, _ = self.step_with_console({}, self.WS_403)
        self.assertFalse(ok)

    def test_other_console_errors_still_fail(self):
        ok, _ = self.step_with_console({"ATLAS_TEST_BYPASS": "b"}, "TypeError: x is undefined")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
