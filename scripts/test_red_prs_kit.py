"""The fleet watches its own fleet-kit PRs, not only the product's (2026-09-29).

On 2026-09-29 four fleet-kit PRs sat stuck 2-20h with nobody on them: #1436 (a new test not
listed in ci.yml), #1421 (a date test main had already fixed), #1392 and #1388 (merge
conflicts). red_prs.py scanned only the product repo and only member/ + minion/ branches; kit
PRs come from dumbledore/ and session branches. And GitHub runs NO checks on a conflicting PR,
so a conflict never even looked red.

RED without the fix: describe() has no kit mode and no CONFLICT state, dispatch_fixer.sh
refuses `kit:N`, and ci_list_merge.py does not exist.
Run: python3 scripts/test_red_prs_kit.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import red_prs  # noqa: E402
from test_red_prs import NOW, check, commit, pr  # noqa: E402


def kit_pr(number=1436, branch="dumbledore/fixer-fanout-cap", mins=120, **kw):
    p = pr(number=number, branch=branch, commits=[commit("the change", mins)], **kw)
    return p


class KitRule(unittest.TestCase):
    def test_any_kit_branch_red_an_hour_is_due(self):
        for branch in ("dumbledore/fixer-fanout-cap", "fix/wake-while-busy-queues-followup",
                       "targeted-tests-only"):
            row = red_prs.describe(kit_pr(branch=branch, checks=[check("selftest", "FAILURE")]),
                                   set(), NOW, kit=True)
            self.assertIsNotNone(row, branch)
            self.assertTrue(row["stalled"])
            self.assertEqual(row["kind"], "fix")

    def test_product_rule_is_unchanged(self):
        p = kit_pr(branch="fix/some-human-branch", checks=[check("test", "FAILURE")])
        self.assertIsNone(red_prs.describe(p, set(), NOW), "a human product branch stays hands-off")

    def test_hands_off_label_and_drafts_are_skipped(self):
        p = kit_pr(checks=[check("selftest", "FAILURE")], labels=["fleet:hands-off"])
        self.assertIsNone(red_prs.describe(p, set(), NOW, kit=True))
        d = kit_pr(checks=[check("selftest", "FAILURE")])
        d["isDraft"] = True
        self.assertIsNone(red_prs.describe(d, set(), NOW, kit=True))

    def test_a_conflict_is_stuck_even_with_no_checks(self):
        p = kit_pr(number=1392, branch="fix/wake-while-busy-queues-followup")
        p["mergeStateStatus"] = "DIRTY"
        row = red_prs.describe(p, set(), NOW, kit=True)
        self.assertIsNotNone(row, "GitHub runs no checks on a conflicting PR; it must still count")
        self.assertEqual(row["state"], "CONFLICT")
        clean = kit_pr(number=1393)
        clean["mergeStateStatus"] = "CLEAN"
        self.assertIsNone(red_prs.describe(clean, set(), NOW, kit=True))

    def test_kit_ledger_is_separate(self):
        self.assertNotEqual(red_prs.ledger_path(kit=True), red_prs.ledger_path())

    def test_open_prs_kit_keeps_every_non_draft_branch(self):
        light = ('[{"number":1,"headRefName":"dumbledore/x","isDraft":false},'
                 '{"number":2,"headRefName":"fix/y","isDraft":false},'
                 '{"number":3,"headRefName":"fix/z","isDraft":true}]')
        seen = []

        def gh(args, timeout=60):
            if args[:2] == ["pr", "list"]:
                return 0, light
            seen.append(int(args[2]))
            return 1, "no"

        red_prs._open_prs("o/kit", gh, kit=True)
        self.assertEqual(sorted(seen), [1, 2])


class Dispatch(unittest.TestCase):
    @unittest.skipUnless(shutil.which("setsid"), "setsid is Linux-only (the box and CI have it)")
    def test_dispatch_fixer_sends_a_kit_pr_as_a_kit_item(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "run_member.sh"
            out = Path(d) / "got"
            fake.write_text(f'#!/bin/bash\necho "$* kit=${{FLEET_FIXER_KIT:-}}" >> {out}\n')
            fake.chmod(0o755)
            env = {**os.environ, "FLEET_RUN_MEMBER": str(fake), "FLEET_LOG_DIR": d,
                   "FLEET_FIXER_ITEM_MAX": "9"}
            r = subprocess.run(["bash", str(HERE / "dispatch_fixer.sh"), "kit:1436", "8805"],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            for _ in range(50):
                if out.exists() and len(out.read_text().splitlines()) == 2:
                    break
                subprocess.run(["sleep", "0.1"])
            got = sorted(out.read_text().splitlines())
            self.assertEqual(got, ["the-fixer --item 1436 kit=1", "the-fixer --item 8805 kit="])

    def test_run_member_claims_a_kit_item_on_the_kit_ledger(self):
        rm = (HERE / "run_member.sh").read_text()
        self.assertIn('red_prs.py" claim "$ITEM" ${FLEET_FIXER_KIT:+--kit}', rm)
        self.assertIn("FIRE assigned-pr ${FLEET_FIXER_KIT:+kit}#$ITEM", rm)

    def test_stop_hook_reads_the_kit_pr_on_a_kit_sub_pass(self):
        import pr_done_hook
        with tempfile.TemporaryDirectory() as d:
            asked = []

            def gh(args, timeout=60):
                asked.append(args[args.index("--repo") + 1] if "--repo" in args else None)
                return 1, "no"

            base = {"FLEET_RUN_ID": "the-fixer-item1436-1-2", "WT_PATH": d, "TMPDIR": d,
                    "FLEET_REPO_SLUG": "o/product", "KIT_REPO_SLUG": "o/kit"}
            pr_done_hook.decide({**base, "FLEET_FIXER_KIT": "1"}, gh=gh)
            pr_done_hook.decide(base, gh=gh)
            self.assertEqual(asked, ["o/kit", "o/product"])

    def test_gru_and_the_fixer_know_the_kit_path(self):
        gru = (HERE.parent / "members/gru/gru.md").read_text()
        self.assertIn("red_prs.py due --kit", gru)
        self.assertIn("dispatch_fixer.sh kit:", gru)
        fixer = (HERE.parent / "members/the-fixer/the-fixer.md").read_text()
        self.assertIn("FIRE assigned-pr kit#<N>", fixer)
        self.assertIn("ci_list_merge.py", fixer)


CONFLICT = """jobs:
  selftest:
    steps:
      - name: run test_a.py
        run: python3 scripts/test_a.py
<<<<<<< HEAD
      - name: run test_mine.py
        run: python3 scripts/test_mine.py
=======
      - name: run test_theirs.py
        run: python3 scripts/test_theirs.py
>>>>>>> origin/main
      - name: run test_z.py
        run: python3 scripts/test_z.py
"""


class CiListMerge(unittest.TestCase):
    def _run(self, text):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ci.yml"
            f.write_text(text)
            r = subprocess.run([sys.executable, str(HERE / "ci_list_merge.py"), str(f)],
                               capture_output=True, text=True)
            return r.returncode, f.read_text()

    def test_both_sides_only_added_test_steps_keeps_both(self):
        rc, out = self._run(CONFLICT)
        self.assertEqual(rc, 0)
        self.assertNotIn("<<<<<<<", out)
        self.assertIn("test_mine.py", out)
        self.assertIn("test_theirs.py", out)

    def test_any_other_conflict_is_left_for_a_person(self):
        other = CONFLICT.replace("        run: python3 scripts/test_theirs.py\n",
                                 "        run: python3 scripts/test_theirs.py --flag\n")
        rc, out = self._run(other)
        self.assertEqual(rc, 1)
        self.assertEqual(out, other, "a refused file is not touched")


if __name__ == "__main__":
    unittest.main()
