#!/usr/bin/env python3
"""test_red_walker.py -- gh#881 acceptance criteria for red_walker.py's --item selector.

Pure unit tests only (criterion 7): no Playwright, no network. select_attacks() and
fetch_item_text() are exercised directly with fixture PRD text and a fixture catalog;
main()'s zero-selection branch (criteria 2 and 3) is exercised by monkeypatching
load_catalog and fetch_item_text so the real gh CLI and PyYAML are never touched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import red_walker as rw  # noqa: E402


FIXTURE_ATTACKS = [
    {"id": "reflect-990", "kind": "reflection", "target": {"path": "/990/"}},
    {"id": "overflow-report", "kind": "overflow", "target": {"path": "/990/report/"}},
    {"id": "idor-org", "kind": "idor",
     "target": {"path": "FIXTURE_OTHER_ORG_ADMIN_URL", "needs": ["FIXTURE_OTHER_ORG_ADMIN_URL"]}},
]


class SelectAttacksTest(unittest.TestCase):
    def test_criterion1_prd_text_contains_path_selects_it(self):
        # "/990/" is itself a substring of "/990/report/", so both attacks whose target path
        # is a substring of the PRD text are selected -- exactly the "best-effort substring
        # match" red_walker.py's own usage text documents; only idor-org's env-var target
        # (not a real path) is absent from this PRD and correctly excluded.
        prd = "## Acceptance criteria\n1. Given /990/report/ renders a claim link..."
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=None)
        self.assertEqual([a["id"] for a in selected], ["reflect-990", "overflow-report"])

    def test_criterion2_no_path_named_selects_nothing(self):
        prd = "## Acceptance criteria\n1. Given the homepage loads..."
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=None)
        self.assertEqual(selected, [])

    def test_criterion4_unscoped_considers_full_catalog(self):
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=None, attacks_filter=None)
        self.assertEqual([a["id"] for a in selected], [a["id"] for a in FIXTURE_ATTACKS])

    def test_criterion6_item_and_attacks_filter_compose(self):
        prd = "/990/ and /990/report/ both matter here"
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=["reflect-990"])
        self.assertEqual([a["id"] for a in selected], ["reflect-990"])

    def test_direction_is_prd_contains_path_not_issue_number_in_target_blob(self):
        # gh#881's bug: an issue number like "573" was checked against json.dumps(target),
        # which can never contain a bare issue number, so every attack was always skipped.
        # Confirm that shape stays gone: an issue number alone, no path, selects nothing.
        prd = "issue 573 with no page path named"
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=None)
        self.assertEqual(selected, [])


class FetchItemTextTest(unittest.TestCase):
    def test_concatenates_body_and_comments(self):
        def fake_run(args, capture_output, text, timeout):
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "body": "the body names /990/report/",
                "comments": [{"body": "a later comment names /990/"}],
            }), stderr="")

        text = rw.fetch_item_text("881", run=fake_run)
        self.assertIn("/990/report/", text)
        self.assertIn("/990/", text)

    def test_criterion5_gh_failure_exits_nonzero_naming_issue(self):
        def fake_run(args, capture_output, text, timeout):
            return SimpleNamespace(returncode=1, stdout="", stderr="could not resolve to an issue")

        with self.assertRaises(SystemExit) as ctx:
            rw.fetch_item_text("999999", run=fake_run)
        self.assertIn("999999", str(ctx.exception))

    def test_criterion5_gh_timeout_exits_nonzero_naming_issue(self):
        import subprocess as sp

        def fake_run(args, capture_output, text, timeout):
            raise sp.TimeoutExpired(cmd=args, timeout=timeout)

        with self.assertRaises(SystemExit) as ctx:
            rw.fetch_item_text("881", run=fake_run)
        self.assertIn("881", str(ctx.exception))


class SelectAttacksEmptyAttacksFilterTest(unittest.TestCase):
    def test_empty_attacks_list_means_no_filter_not_zero_selection(self):
        # argparse's `nargs="*"` gives [] for a bare `--attacks` with no values; that must
        # keep meaning "no --attacks filter", same as the old truthiness check did, not
        # "filter to nothing."
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=None, attacks_filter=[])
        self.assertEqual([a["id"] for a in selected], [a["id"] for a in FIXTURE_ATTACKS])


class RunAttackExceptionHandlingTest(unittest.TestCase):
    """gh#5469 criteria 1, 4, 5, 6: a runner exception -- Playwright's TimeoutError or any
    other -- must raise Blocked out of run_attack(), never get swallowed into a fake "pass"
    step (red_walker.py:239 as it read before this fix). Stand-in TimeoutError/ValueError are
    used here since run_attack() catches Exception generically, with no Playwright-specific
    branch, so a plain builtin exception exercises the identical code path without needing a
    real Playwright install for this pure-unit test file.

    This class is RED against unpatched red_walker.py:239 (the assertRaises(Blocked) calls
    fail because the old code swallowed the exception and returned a "pass" step instead) and
    GREEN after the fix -- run `python3 scripts/test_red_walker.py` before and after."""

    class _FakePage:
        def screenshot(self, path):
            pass

    class _FakeCtx:
        def __init__(self):
            self.closed = False

        def new_page(self):
            return RunAttackExceptionHandlingTest._FakePage()

        def route(self, pattern, handler):
            pass

        def close(self):
            self.closed = True

    class _FakeBrowser:
        def __init__(self):
            self.ctx = RunAttackExceptionHandlingTest._FakeCtx()

        def new_context(self, viewport):
            return self.ctx

    @staticmethod
    def _attack():
        return {"id": "fixture-attack", "kind": "_fixture-kind", "name": "Fixture attack",
                "steps": [{"action": "goto", "landed_when": "some payload executes"}]}

    def _run(self, runner_fn):
        rw.RUNNERS["_fixture-kind"] = runner_fn
        browser = self._FakeBrowser()
        with tempfile.TemporaryDirectory() as tmp:
            result = rw.run_attack(self._attack(), rw.Config({}), browser, Path(tmp), "run1",
                                    "desktop", {"width": 100, "height": 100})
        return result, browser

    def test_criterion1_timeout_raises_blocked_not_a_fake_pass(self):
        def timeout_runner(page, cfg, attack, step):
            raise TimeoutError("Timeout 20000ms exceeded.")

        with self.assertRaises(rw.Blocked) as ctx:
            self._run(timeout_runner)
        self.assertIn("TimeoutError", str(ctx.exception))

    def test_criterion4_a_genuine_pass_is_unaffected(self):
        def clean_runner(page, cfg, attack, step):
            return False, "nothing found"

        result, browser = self._run(clean_runner)
        self.assertEqual(result["steps"][0]["status"], "pass")
        self.assertEqual(result["steps"][0]["detail"], "nothing found")
        self.assertTrue(browser.ctx.closed)  # context still closes on the clean path

    def test_criterion5_a_non_timeout_exception_also_routes_to_blocked_with_its_type(self):
        def buggy_runner(page, cfg, attack, step):
            raise ValueError("boom")

        with self.assertRaises(rw.Blocked) as ctx:
            self._run(buggy_runner)
        self.assertIn("ValueError", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_context_still_closes_when_the_runner_times_out(self):
        def timeout_runner(page, cfg, attack, step):
            raise TimeoutError("Timeout 20000ms exceeded.")

        rw.RUNNERS["_fixture-kind"] = timeout_runner
        browser = self._FakeBrowser()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                rw.run_attack(self._attack(), rw.Config({}), browser, Path(tmp), "run1",
                               "desktop", {"width": 100, "height": 100})
            except rw.Blocked:
                pass
        self.assertTrue(browser.ctx.closed)


class ExitCodeTest(unittest.TestCase):
    """Criteria 2 and 3, isolated from Playwright: _exit_code() is the pure function main()
    now delegates the summary/exit-code decision to."""

    def test_criterion3_every_selected_attack_blocked_is_nonzero(self):
        # e.g. 3 of 4 attacks time out and the 4th is missing a fixture -- nothing genuinely
        # ran, so a caller must not read exit 0 as a clean sweep.
        selected = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertNotEqual(rw._exit_code(landed=0, selected=selected, attacks_out=[]), 0)

    def test_a_real_landed_finding_always_wins(self):
        self.assertEqual(rw._exit_code(landed=1, selected=[{"id": "a"}], attacks_out=[{"id": "a"}]), 1)

    def test_criterion2_a_clean_run_with_some_real_passes_is_zero(self):
        # 3 of 4 selected time out (blocked), the 4th genuinely runs and finds nothing --
        # the summary reports "1 attacks, 0 landed, 3 blocked", exit 0, per criterion 2's shape.
        selected = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
        attacks_out = [{"id": "d"}]
        self.assertEqual(rw._exit_code(landed=0, selected=selected, attacks_out=attacks_out), 0)

    def test_nothing_selected_is_zero_here_too(self):
        # main()'s own zero-selection branch (MainZeroSelectionTest) returns 2 before this
        # function is ever called; _exit_code itself must not invent a different code for
        # the empty-selected case.
        self.assertEqual(rw._exit_code(landed=0, selected=[], attacks_out=[]), 0)


class MainZeroSelectionTest(unittest.TestCase):
    """Criteria 2 and 3: a scoped run that selects zero attacks writes a distinguishing
    field into the same results object vp reads (red_walker.py:307) and exits non-zero --
    never the 0 landed/0 blocked shape a clean run also produces."""

    def test_zero_selection_is_nonzero_exit_with_reason_in_results(self):
        orig_argv, orig_load, orig_fetch = sys.argv, rw.load_catalog, rw.fetch_item_text
        rw.load_catalog = lambda path: {"attacks": FIXTURE_ATTACKS, "viewports": {}}
        rw.fetch_item_text = lambda item: "no matching path named here"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sys.argv = ["red_walker.py", "--item", "573", "--out", tmp]
                rc = rw.main()
                results_files = list(Path(tmp).rglob("results.json"))
                self.assertEqual(len(results_files), 1)
                data = json.loads(results_files[0].read_text())
        finally:
            sys.argv, rw.load_catalog, rw.fetch_item_text = orig_argv, orig_load, orig_fetch

        self.assertNotEqual(rc, 0)
        self.assertEqual(data["selected"], 0)
        self.assertIn("573", data["reason"])
        self.assertEqual(data["summary"], {"attacks": 0, "landed": 0, "blocked": 0})


if __name__ == "__main__":
    unittest.main()
