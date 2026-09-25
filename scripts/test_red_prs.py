"""The red half of the merge-stall detector, and the dispatch that fixes what it finds.

2026-09-25, philanthropy: #7975, #7982, #7986 (all fleet:reif-priority builds) sat red for
hours. Three things had to be true for that, and each is pinned here:

  1. "red" had no shared definition that saw lint, a review BLOCK that outlives a sync merge,
     or the difference between a real fix and auto_update_branch.sh's catch-up merge
     (pr_ci_wait.classify);
  2. nothing routed a red PR to anyone who acts: the stall alarm only sees GREEN PRs
     (red_prs.describe/plan);
  3. the-fixer's --item sub-passes were muted by the parent's own pregate dedup (#8002), so
     even the fan-out that existed did nothing (run_member.sh's pregate block).

Run: python3 scripts/test_red_prs.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pr_ci_wait  # noqa: E402
import red_prs  # noqa: E402

NOW = time.time()


def iso(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))


def check(name, conclusion="SUCCESS", status="COMPLETED", mins=5, url=""):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "startedAt": iso(mins + 2), "completedAt": iso(mins) if status == "COMPLETED" else None,
            "detailsUrl": url or f"https://github.com/o/r/actions/runs/1/job/{abs(hash(name)) % 999}"}


def commit(headline, mins, oid=None):
    return {"oid": oid or f"{abs(hash((headline, mins))):040x}"[:40], "messageHeadline": headline,
            "committedDate": iso(mins)}


def pr(number=7986, branch="member/minion-item7942_7937-79476-1790348769", checks=(),
       commits=(), comments=(), labels=(), state="OPEN", armed=True):
    return {"number": number, "state": state, "headRefName": branch, "headRefOid": "c4ae2797da7a" + "0" * 28,
            "isDraft": False, "autoMergeRequest": {} if armed else None,
            "statusCheckRollup": list(checks), "commits": list(commits),
            "comments": list(comments), "labels": [{"name": n} for n in labels],
            "createdAt": iso(300), "url": f"https://github.com/o/r/pull/{number}"}


GREEN = [check("test"), check("test-postgres"), check("lint")]


class Classify(unittest.TestCase):
    def test_lint_red_is_red_even_though_lint_is_not_required(self):
        # #7986: lint (not a required check) failed on ruff I001 and nobody treated it as red.
        info = pr_ci_wait.classify(pr(checks=[check("test"), check("test-postgres"),
                                              check("lint", "FAILURE")]))
        self.assertEqual(info["state"], "RED")
        self.assertEqual(info["failed"], ["lint"])

    def test_newest_attempt_wins(self):
        old_red = check("test", "FAILURE", mins=30)
        new_green = check("test", "SUCCESS", mins=2)
        self.assertEqual(pr_ci_wait.classify(pr(checks=[old_red, new_green, check("lint")]))["state"],
                         "GREEN")

    def test_running_check_is_pending_and_no_checks_is_pending(self):
        self.assertEqual(pr_ci_wait.classify(pr(checks=[check("test", None, "IN_PROGRESS")]))["state"],
                         "PENDING")
        self.assertEqual(pr_ci_wait.classify(pr(checks=[]))["state"], "PENDING")

    def test_review_block_survives_a_sync_merge_but_not_a_real_fix(self):
        # #7975: BLOCK at 15:32 on 347b423, auto_update_branch merged main at 15:50 -> new head,
        # no fleet-code-review status on it, findings untouched. Still blocked.
        block = {"body": "**fleet-code-review: BLOCK** (local claude, head 347b423536e6)\n\n- medium -- x",
                 "createdAt": iso(20)}
        synced = pr(checks=GREEN, comments=[block],
                    commits=[commit("Lifecycle emails: guard (gh#7947)", 170),
                             commit("Merge branch 'main' into member/minion-item7947", 2)])
        info = pr_ci_wait.classify(synced)
        self.assertEqual(info["state"], "BLOCK")
        self.assertIn("medium -- x", info["review_findings"])
        fixed = pr(checks=GREEN, comments=[block],
                   commits=[commit("Lifecycle emails: guard (gh#7947)", 170),
                            commit("Address review: retry stuck sends", 5)])
        self.assertEqual(pr_ci_wait.classify(fixed)["state"], "GREEN")

    def test_review_status_failure_on_head_blocks(self):
        status = {"__typename": "StatusContext", "context": "fleet-code-review", "state": "FAILURE"}
        self.assertEqual(pr_ci_wait.classify(pr(checks=GREEN + [status]))["state"], "BLOCK")

    def test_last_real_commit_skips_sync_merges(self):
        cs = [commit("Real work", 90, "a" * 40), commit("Merge branch 'main' into x", 10),
              commit("Merge remote-tracking branch 'origin/main' into x", 5)]
        self.assertEqual(pr_ci_wait.last_real_commit(cs)["oid"], "a" * 40)

    def test_exit_codes(self):
        self.assertEqual(pr_ci_wait.EXIT["GREEN"], 0)
        self.assertEqual(pr_ci_wait.EXIT["RED"], 1)
        self.assertEqual(pr_ci_wait.EXIT["BLOCK"], 1)
        self.assertEqual(pr_ci_wait.EXIT["PENDING"], 3)

    def test_wait_polls_until_not_pending(self):
        seq = [pr(checks=[check("test", None, "IN_PROGRESS")]), pr(checks=GREEN)]
        calls = []

        def gh(args, timeout=60):
            calls.append(args)
            return 0, json.dumps(seq[min(len(calls) - 1, 1)])
        info = pr_ci_wait.wait(7986, None, timeout=600, interval=1, gh=gh, sleep=lambda s: None)
        self.assertEqual(info["state"], "GREEN")
        self.assertEqual(len(calls), 2)


class Detector(unittest.TestCase):
    RED_LINT = [check("test"), check("test-postgres"), check("lint", "FAILURE")]

    def test_reif_priority_pr_is_due_at_once(self):
        row = red_prs.describe(pr(checks=self.RED_LINT, commits=[commit("work", 10)]), {7942}, NOW)
        self.assertTrue(row["reif_priority"])
        self.assertFalse(row["stalled"])
        self.assertEqual([r["number"] for r in red_prs.plan([row], {}, NOW, 6)["due"]], [7986])

    def test_other_red_pr_waits_until_60_minutes_without_a_real_push(self):
        fresh = red_prs.describe(pr(number=1, checks=self.RED_LINT, commits=[commit("w", 30)]), set(), NOW)
        old = red_prs.describe(pr(number=2, checks=self.RED_LINT,
                                  commits=[commit("w", 61), commit("Merge branch 'main' into x", 3)]),
                               set(), NOW)
        self.assertFalse(fresh["stalled"])
        self.assertTrue(old["stalled"], "a sync merge must not reset the red clock")
        out = red_prs.plan([fresh, old], {}, NOW, 6)
        self.assertEqual([r["number"] for r in out["due"]], [2])
        self.assertEqual(out["not_yet_stalled"], [1])

    def test_green_draft_and_human_branches_are_ignored(self):
        self.assertIsNone(red_prs.describe(pr(checks=GREEN), {7942}, NOW))
        self.assertIsNone(red_prs.describe(pr(branch="fix/reif-hand-edit", checks=self.RED_LINT), set(), NOW))
        d = pr(checks=self.RED_LINT)
        d["isDraft"] = True
        self.assertIsNone(red_prs.describe(d, set(), NOW))

    def test_reif_first_then_oldest(self):
        rows = [red_prs.describe(pr(number=n, branch=f"member/minion-item{i}-1-2", checks=self.RED_LINT,
                                    commits=[commit("w", age)]), {7000}, NOW)
                for n, i, age in [(10, 1, 400), (11, 7000, 5), (12, 2, 900)]]
        self.assertEqual([r["number"] for r in red_prs.plan(rows, {}, NOW, 6)["due"]], [11, 12, 10])
        self.assertEqual(len(red_prs.plan(rows, {}, NOW, 1)["due"]), 1)

    def test_items_parse_from_branch(self):
        self.assertEqual(red_prs.items_of("member/minion-item7942_7950_7948-5046-1790343860"),
                         [7942, 7950, 7948])
        self.assertEqual(red_prs.items_of("member/the-fixer-112974-1790351239"), [])


class Dedup(unittest.TestCase):
    def test_same_content_is_not_resent_inside_the_window_and_gives_up_after_three(self):
        ledger = {}
        self.assertEqual(red_prs.verdict(ledger.get("7986"), "abc", NOW), "go")
        red_prs.record(ledger, 7986, "abc", NOW)
        self.assertEqual(red_prs.verdict(ledger["7986"], "abc", NOW + 60), "recent")
        later = NOW + red_prs.REDISPATCH_MIN * 60 + 1
        self.assertEqual(red_prs.verdict(ledger["7986"], "abc", later), "go")
        red_prs.record(ledger, 7986, "abc", later)
        red_prs.record(ledger, 7986, "abc", later * 2)
        self.assertEqual(red_prs.verdict(ledger["7986"], "abc", later * 3), "exhausted")

    def test_a_real_push_resets_the_count(self):
        ledger = {}
        for _ in range(3):
            red_prs.record(ledger, 1, "abc", NOW)
        self.assertEqual(red_prs.verdict(ledger["1"], "def", NOW), "go")
        self.assertEqual(red_prs.record(ledger, 1, "def", NOW)["attempts"], 1)

    def test_plan_holds_and_exhausts(self):
        row = red_prs.describe(pr(checks=Detector.RED_LINT, commits=[commit("w", 10, "c" * 40)]),
                               {7942}, NOW)
        out = red_prs.plan([row], {"7986": {"content": "c" * 40, "attempts": 1, "last": NOW}}, NOW, 6)
        self.assertEqual(out["due"], [])
        self.assertEqual(out["held"], [{"number": 7986, "why": "recent"}])
        out = red_prs.plan([row], {"7986": {"content": "c" * 40, "attempts": 3, "last": 0}}, NOW, 6)
        self.assertEqual(out["exhausted"], [7986])

    def test_claim_cli_records_then_skips(self):
        red = pr(checks=Detector.RED_LINT, commits=[commit("w", 10, "d" * 40)])
        with tempfile.TemporaryDirectory() as d:
            os.environ["FLEET_LOG_DIR"] = d
            orig = pr_ci_wait.fetch
            pr_ci_wait.fetch = lambda n, repo, gh=None: red
            try:
                self.assertEqual(red_prs.main(["claim", "7986"]), 0)
                self.assertEqual(red_prs.main(["claim", "7986"]), 1)
                data = json.loads((Path(d) / "red_pr_dispatch.json").read_text())
                self.assertEqual(data["7986"]["attempts"], 1)
                pr_ci_wait.fetch = lambda n, repo, gh=None: None
                self.assertEqual(red_prs.main(["claim", "7986"]), 0, "unreadable PR fails open")
            finally:
                pr_ci_wait.fetch = orig
                os.environ.pop("FLEET_LOG_DIR", None)


class RunMemberPregate(unittest.TestCase):
    """#8002: `run_member.sh the-fixer --item N` ran check.sh, which answered
    `green (already-fighting ...)` because the PARENT had just recorded the batch, and the
    sub-pass exited $0 without a model. Run the real pregate block from run_member.sh with the
    real shape of that pregate answer and prove a dispatched --item reaches the model."""

    def _block(self) -> str:
        text = (HERE / "run_member.sh").read_text()
        start = text.index('PREGATE=$(jget "[\'llm\'].get(\'pregate\', \'\')")')
        m = re.compile(r"^fi$", re.M).search(text, start)
        return text[start:m.end()]

    def _run(self, item: str, claim_rc: int = 0) -> subprocess.CompletedProcess:
        d = tempfile.mkdtemp()
        kit = Path(d) / "kit"
        (kit / "members" / "the-fixer").mkdir(parents=True)
        (kit / "scripts").mkdir()
        (kit / "members" / "the-fixer" / "check.sh").write_text(
            "echo ran >> \"$MARK\"; echo 'green (already-fighting batch:7986:ff0f44c:check-failed)'\n")
        (kit / "scripts" / "red_prs.py").write_text(
            f"import sys\nprint('red_prs: stub claim ' + ' '.join(sys.argv[1:]))\nsys.exit({claim_rc})\n")
        (kit / "scripts" / "run_report.py").write_text("import sys\nsys.stdin.read()\n")
        harness = (f'set -u\nKIT_DIR="{kit}"; LOG_DIR="{d}"; LOG="{d}/log"; MEMBER=the-fixer; REPO="{d}"\n'
                   f'ITEM="{item}"; DRY_RUN=0; LANE_FLAG=""; export MARK="{d}/mark"\n'
                   'jget() { echo "members/the-fixer/check.sh"; }\n'
                   'log() { echo "LOG: $*"; }\n'
                   + self._block() +
                   '\necho "REACHED_MODEL pregate=$FLEET_PREGATE_OUTPUT"\n'
                   f'[ -f "{d}/mark" ] && echo CHECK_SH_RAN\n')
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)

    def test_dispatched_item_skips_the_pregate_and_reaches_the_model(self):
        r = self._run("7986")
        self.assertIn("REACHED_MODEL pregate=FIRE assigned-pr #7986", r.stdout, r.stderr)
        self.assertNotIn("CHECK_SH_RAN", r.stdout, "check.sh must not run for a dispatched item")

    def test_dedup_skip_exits_quiet_without_the_model(self):
        r = self._run("7986", claim_rc=1)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("REACHED_MODEL", r.stdout)

    def test_the_hourly_pass_still_obeys_its_pregate(self):
        r = self._run("")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("REACHED_MODEL", r.stdout, "no --item: a green pregate still stops at $0")

    def test_fixer_prompt_names_a_pull_request_not_an_issue(self):
        text = (HERE / "run_member.sh").read_text()
        self.assertIn('Your assigned PULL REQUEST for this run is #$ITEM', text)


if __name__ == "__main__":
    unittest.main()
