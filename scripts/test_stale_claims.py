"""fleet:claimed is a lease: a claim nobody is working is released at the start of gru's pass.

2026-09-25, philanthropy: gru's 13:38 UTC pass claimed #7937-#7942, #7948, #7950 for one minion
batch. The minion died; only #7982 (open, red) and #7986 came out, and fleet:claimed sat on the
rest for 12 hours while every gru pass skipped them as claimed ("no open item passed both
gates"). M removed the label by hand at ~01:30 UTC 09-26.

Pinned here: that exact shape is released after the lease and not before; a live runner, a real
commit on an open PR, or a fresh branch holds the claim, a catch-up merge from main does not;
marie's merged-PR hold is honored; a failed PR read releases nothing; releases comment, unlabel
and log; and gru's pass start + report are wired to it.

Run: python3 scripts/test_stale_claims.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(HERE))

import stale_claims as sc  # noqa: E402

CLAIM_AT = "2026-09-25T13:43:41Z"
NOW = sc._ts("2026-09-26T01:40:00Z")
BATCH = [7942, 7950, 7948, 7941, 7940, 7939, 7938, 7937]
REIF = [{"name": "fleet:backlog"}, {"name": "fleet:reif-priority"}, {"name": "fleet:claimed"}]


def issue(n, claimed_at=CLAIM_AT, extra=(), labels=REIF):
    return {"number": n, "labels": list(labels), "updatedAt": claimed_at,
            "comments": [{"createdAt": claimed_at, "body": "claimed-by: gru (orchestrator pass gru-299)"},
                         *extra]}


def pr_7982(last_real_at):
    return {"number": 7982, "title": "Reif-priority batch",
            "headRefName": "member/minion-item" + "_".join(map(str, BATCH)) + "-5046-1790343860",
            "body": "", "createdAt": "2026-09-25T14:10:00Z", "last_real_at": last_real_at}


class Assess(unittest.TestCase):
    def test_the_incident_is_released_after_the_lease(self):
        prs = [pr_7982(sc._ts("2026-09-25T14:20:00Z"))]
        for n in BATCH:
            r = sc.assess(issue(n), prs, [], set(), NOW)
            self.assertTrue(r["release"], (n, r))
            self.assertEqual(r["evidence"]["open_prs"], [7982])

    def test_inside_the_lease_nothing_is_released(self):
        r = sc.assess(issue(7937), [], [], set(), sc._ts(CLAIM_AT) + 59 * 60)
        self.assertFalse(r["release"])
        self.assertEqual(r["kind"], "inside-lease")
        self.assertTrue(sc.assess(issue(7937), [], [], set(), sc._ts(CLAIM_AT) + 61 * 60)["release"])

    def test_a_live_minion_holds_its_items(self):
        live = sc.numbers_in_argv(["bash", "/fleet-kit/scripts/run_member.sh", "minion",
                                   "--items", "7937,7938,7939"])
        self.assertEqual(live, {7937, 7938, 7939})
        self.assertEqual(sc.assess(issue(7938), [], [], live, NOW)["kind"], "live-runner")
        self.assertTrue(sc.assess(issue(7940), [], [], live, NOW)["release"])

    def test_a_live_fixer_on_the_pr_holds_the_prs_items(self):
        live = sc.numbers_in_argv(["bash", "/fleet-kit/scripts/run_member.sh", "the-fixer", "--item", "7982"])
        r = sc.assess(issue(7940), [pr_7982(None)], [], live, NOW)
        self.assertFalse(r["release"])
        self.assertIn("#7982", r["why"])

    def test_non_runner_processes_never_count(self):
        self.assertEqual(sc.numbers_in_argv(["vim", "--item", "7937"]), set())
        self.assertEqual(sc.numbers_in_argv([]), set())
        self.assertEqual(sc.numbers_in_argv(["bash", "/fleet-kit/scripts/dispatch_fixer.sh", "7975", "7982"]),
                         {7975, 7982})

    def test_a_recent_real_commit_on_an_open_pr_holds(self):
        r = sc.assess(issue(7937), [pr_7982(NOW - 20 * 60)], [], set(), NOW)
        self.assertEqual(r["kind"], "pr-activity")

    def test_a_pr_that_only_mentions_it_in_the_body_counts(self):
        pr = {"number": 8001, "title": "x", "headRefName": "member/ui-lane", "body": "Part of gh#7937",
              "createdAt": "2026-09-25T14:00:00Z", "last_real_at": NOW - 5 * 60}
        self.assertFalse(sc.assess(issue(7937), [pr], [], set(), NOW)["release"])
        self.assertTrue(sc.assess(issue(79370), [pr], [], set(), NOW)["release"])  # word-bounded

    def test_a_fresh_branch_without_a_pr_holds(self):
        b = [{"name": "member/minion-item7939-1-2", "committed_at": NOW - 10 * 60}]
        self.assertEqual(sc.assess(issue(7939), [], b, set(), NOW)["kind"], "branch-activity")
        b = [{"name": "member/minion-item7939-1-2", "committed_at": NOW - 3 * 3600}]
        self.assertTrue(sc.assess(issue(7939), [], b, set(), NOW)["release"])

    def test_marie_merged_pr_hold_is_honored_until_a_newer_claim(self):
        note = {"createdAt": "2026-09-25T15:00:00Z",
                "body": "marie: fleet:claimed left in place — merged PR #7986 references this issue"}
        self.assertEqual(sc.assess(issue(7937, extra=[note]), [], [], set(), NOW)["kind"], "marie-merged-pr")
        reclaim = {"createdAt": "2026-09-25T16:06:43Z", "body": "claimed-by: gru (orchestrator pass gru-123939)"}
        self.assertTrue(sc.assess(issue(7937, extra=[note, reclaim]), [], [], set(), NOW)["release"])

    def test_an_idle_checkpoint_draft_releases_inside_the_lease(self):
        # 2026-09-26 14:17 UTC: #7937/#7938/#7941 claimed behind checkpoint drafts
        # #8152/#8134/#8136, no minion running, held as pr-activity/inside-lease.
        now = sc._ts("2026-09-26T14:17:00Z")
        ckpt = {"number": 8152, "title": "WIP (minion checkpoint): #7937",
                "headRefName": "member/minion-item7937-123018-1790425867",
                "body": "<!-- fleet-checkpoint -->", "createdAt": "2026-09-26T13:29:19Z",
                "last_real_at": now - 48 * 60}
        r = sc.assess(issue(7937, claimed_at="2026-09-26T13:55:00Z"), [ckpt], [], set(), now)
        self.assertTrue(r["release"], r)
        self.assertEqual(r["kind"], "checkpoint-idle")
        self.assertIn("#8152", sc.release_note(r))
        # A live minion resuming it holds the claim.
        self.assertFalse(sc.assess(issue(7937, claimed_at="2026-09-26T13:55:00Z"), [ckpt], [],
                                   {7937}, now)["release"])
        # Inside the claim-to-spawn grace it holds.
        self.assertFalse(sc.assess(issue(7937, claimed_at="2026-09-26T14:12:00Z"), [ckpt], [],
                                   set(), now)["release"])
        # A non-checkpoint PR with a fresh real commit still holds.
        plain = dict(ckpt, title="Part of #7937", body="", last_real_at=now - 5 * 60)
        self.assertEqual(sc.assess(issue(7937, claimed_at="2026-09-26T13:00:00Z"), [plain], [],
                                   set(), now)["kind"], "pr-activity")

    def test_close_verify_is_held(self):
        lb = REIF + [{"name": "fleet:needs-close-verify"}]
        self.assertEqual(sc.assess(issue(7937, labels=lb), [], [], set(), NOW)["kind"], "close-verify")

    def test_eligibility(self):
        self.assertTrue(sc.is_eligible(issue(1)))
        self.assertTrue(sc.is_eligible(issue(1, labels=[{"name": "fleet:backlog"}, {"name": "fleet:priority-low"}])))
        self.assertFalse(sc.is_eligible(issue(1, labels=[{"name": "fleet:backlog"}])))
        self.assertFalse(sc.is_eligible(issue(1, labels=REIF + [{"name": "fleet:needs-human-op"}])))


def fake_gh(issues, light_prs, full_prs, pr_rc=0, merged=()):
    def gh(args, timeout=60):
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(issues)
        if args[:2] == ["pr", "list"] and "merged" in args:
            return 0, json.dumps(merged)
        if args[:2] == ["pr", "list"]:
            return pr_rc, json.dumps(light_prs) if pr_rc == 0 else "HTTP 502"
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps(full_prs[int(args[2])])
        if args[:2] == ["repo", "view"]:
            return 0, json.dumps({"owner": {"login": "PhilanthropyOrg"}, "name": "philanthropy"})
        if args[:2] == ["api", "graphql"]:
            return 0, json.dumps({"data": {"repository": {"refs": {"nodes": []}}}})
        raise AssertionError(args)
    return gh


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLEET_LOG_DIR"] = self.tmp.name
        light = [{"number": 7982, "title": "batch", "body": "",
                  "headRefName": pr_7982(None)["headRefName"], "createdAt": "2026-09-25T14:10:00Z"}]
        full = {7982: {"commits": [
            {"messageHeadline": "feat: sector board bars", "committedDate": "2026-09-25T14:20:00Z"},
            # auto_update_branch.sh catches it up with main every tick: not work on the PR
            {"messageHeadline": "Merge branch 'main' into member/minion-item7942", "committedDate": "2026-09-26T01:30:00Z"},
        ]}}
        issues = [issue(n) for n in BATCH] + [issue(9001, labels=[{"name": "fleet:claimed"}])]
        merged = [{"number": 7986, "title": "profile bio", "headRefName": "member/ui-lane",
                   "body": "Part of #7942. Remaining: live prod check.\nNot built: #7937 -- untouched."}]
        self.gh = fake_gh(issues, light, full, merged=merged)
        self.issues, self.light, self.full = issues, light, full
        self.cmds = []

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FLEET_LOG_DIR", None)

    def run_cmd(self, cmd):
        self.cmds.append(cmd)
        return 0, ""

    def test_sweep_releases_comments_and_logs(self):
        res = sc.sweep("PhilanthropyOrg/philanthropy", False, self.gh, NOW, live={7940, 7948}, run=self.run_cmd)
        self.assertEqual(res["released"], [7937, 7938, 7939, 7941, 7942, 7950, 9001])
        self.assertEqual(res["released_eligible"], [7937, 7938, 7939, 7941, 7942, 7950])
        self.assertEqual(res["held_eligible"], [7940, 7948])
        self.assertEqual(res["held_eligible_by_reason"], {"live-runner": [7940, 7948]})
        edits = [c for c in self.cmds if c[:3] == ["gh", "issue", "edit"]]
        self.assertEqual(len(edits), 7)
        self.assertIn("--remove-label", edits[0])
        self.assertEqual(edits[0][-2:], ["--repo", "PhilanthropyOrg/philanthropy"])
        note = next(c for c in self.cmds if c[:3] == ["gh", "issue", "comment"])[5]
        self.assertIn("lease expired", note)
        self.assertIn("#7982", note)
        notes = {c[3]: c[5] for c in self.cmds if c[:3] == ["gh", "issue", "comment"]}
        self.assertIn("Merged PR(s) #7986", notes["7942"])
        self.assertIn("`Remaining:`", notes["7942"])
        self.assertNotIn("Merged PR", notes["7950"])
        log = (Path(self.tmp.name) / "claim_leases.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 7)
        self.assertEqual(json.loads(log[0])["event"], "released")
        self.assertIn("2 eligible items still held as claimed", sc.summary(res))

    def test_dry_run_changes_nothing(self):
        res = sc.sweep(None, True, self.gh, NOW, live=set(), run=self.run_cmd)
        self.assertEqual(len(res["released"]), 9)
        self.assertEqual(self.cmds, [])
        self.assertFalse((Path(self.tmp.name) / "claim_leases.jsonl").exists())

    def test_unreadable_pr_list_releases_nothing(self):
        gh = fake_gh(self.issues, self.light, self.full, pr_rc=1)
        res = sc.sweep(None, False, gh, NOW, live=set(), run=self.run_cmd)
        self.assertIn("error", res)
        self.assertEqual(self.cmds, [])

    def test_a_failed_release_stays_counted_as_held(self):
        res = sc.sweep(None, False, self.gh, NOW, live=set(), run=lambda c: (1, "boom"))
        self.assertEqual(res["released"], [])
        self.assertEqual(len(res["release_failed"]), 9)
        self.assertEqual(res["held_eligible_by_reason"], {"release-failed": sorted(BATCH)})


class Wiring(unittest.TestCase):
    def test_gru_pass_starts_with_the_sweep(self):
        fan = (KIT / "scripts" / "run_gru_fanout.sh").read_text()
        self.assertLess(fan.index("stale_claims.py\" release"), fan.index("exec bash"))

    def test_gru_reads_it_and_reports_the_claimed_skip_count(self):
        gru = (KIT / "members" / "gru" / "gru.md").read_text()
        step0 = gru[gru.index("0. **Your own red PRs first"):gru.index("1. **Read this hour's allowance")]
        self.assertIn("python3 /fleet-kit/scripts/stale_claims.py last", step0)
        report = gru[gru.index("## Report"):]
        self.assertIn("Skipped as claimed:", report)
        self.assertIn("held_eligible", report)

    def test_ci_runs_this_file(self):
        self.assertIn("scripts/test_stale_claims.py", (KIT / ".github" / "workflows" / "ci.yml").read_text())


if __name__ == "__main__":
    unittest.main()
