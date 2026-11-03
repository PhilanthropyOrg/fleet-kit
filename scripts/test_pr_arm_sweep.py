#!/usr/bin/env python3
"""Tests for pr_arm_sweep's pure core -- the real PR shapes measured on 2026-09-12.

Every case below is a shape that actually occurred in the sweep that motivated the script, not
an invented one: a green unarmed kit PR sitting 26h (fleet-kit#964/#966), a green unarmed
product PR sitting 20h (philanthropy#5447), an already-armed PR (32 of 45), and the two ways a
sweep could do harm if it were naive -- arming a PR whose author is still mid-pass, and arming
something a human parked.

Run: python3 scripts/test_pr_arm_sweep.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pr_arm_sweep  # noqa: E402

NOW = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()
REQUIRED = ["selftest"]

ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except AssertionError as exc:
        fail.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


def _pr(number=1, *, age_h=26.0, draft=False, armed=False, labels=(), mergeable="MERGEABLE",
        checks=(("selftest", "SUCCESS"),), state="OPEN"):
    created = (NOW - timedelta(hours=age_h)).isoformat().replace("+00:00", "Z")
    rollup = []
    for name, conclusion in checks:
        if conclusion in ("QUEUED", "IN_PROGRESS"):
            rollup.append({"name": name, "status": conclusion, "conclusion": None})
        else:
            rollup.append({"name": name, "status": "COMPLETED", "conclusion": conclusion,
                           "completedAt": created})
    return {"number": number, "isDraft": draft, "createdAt": created, "state": state,
            "autoMergeRequest": {"enabledAt": created} if armed else None,
            "labels": [{"name": n} for n in labels], "mergeable": mergeable,
            "statusCheckRollup": rollup}


def test_green_unarmed_kit_pr_is_armed():
    """fleet-kit#966: 26.3h old, selftest SUCCESS, autoMergeRequest null, not draft."""
    decision, why = pr_arm_sweep.should_arm(_pr(966), REQUIRED, NOW_TS)
    assert decision is True, why
    assert "green on selftest" in why, why


def test_already_armed_is_left_alone():
    decision, why = pr_arm_sweep.should_arm(_pr(5484, armed=True), REQUIRED, NOW_TS)
    assert decision is False and why == "already armed", why


def test_draft_is_left_alone():
    decision, why = pr_arm_sweep.should_arm(_pr(7, draft=True), REQUIRED, NOW_TS)
    assert decision is False and why == "draft", why


def test_young_pr_is_not_raced_to_its_own_arm():
    """A pass that opened a PR 5 minutes ago may still be inside its own step 9."""
    decision, why = pr_arm_sweep.should_arm(_pr(8, age_h=5 / 60), REQUIRED, NOW_TS)
    assert decision is False and "author may still be arming" in why, why


def test_hold_label_is_respected():
    for label in ("fleet:needs-human-op", "do-not-merge", "fleet:hold"):
        decision, why = pr_arm_sweep.should_arm(_pr(9, labels=(label,)), REQUIRED, NOW_TS)
        assert decision is False and "hold label" in why, (label, why)


def test_failing_required_check_is_not_armed():
    decision, why = pr_arm_sweep.should_arm(
        _pr(10, checks=(("selftest", "FAILURE"),)), REQUIRED, NOW_TS)
    assert decision is False and "is FAILURE" in why, why


def test_pending_required_check_is_not_armed():
    decision, why = pr_arm_sweep.should_arm(
        _pr(11, checks=(("selftest", "IN_PROGRESS"),)), REQUIRED, NOW_TS)
    assert decision is False and "IN_PROGRESS" in why, why



def test_judge_judy_block_is_not_armed():
    """fleet-kit#964/#966 carried fleet-code-review FAILURE with selftest green; arming would merge them."""
    for state in ("FAILURE", "ERROR"):
        pr = _pr(964)
        pr["statusCheckRollup"].append({"context": "fleet-code-review", "state": state})
        decision, why = pr_arm_sweep.should_arm(pr, REQUIRED, NOW_TS)
        assert decision is False and "judge-judy blocked" in why, (state, why)
    pr = _pr(964)
    pr["statusCheckRollup"].append({"context": "fleet-code-review", "state": "SUCCESS"})
    assert pr_arm_sweep.should_arm(pr, REQUIRED, NOW_TS)[0] is True

def test_missing_required_check_is_not_armed():
    """A required context that never reported is not a green PR -- it is a stuck one."""
    decision, why = pr_arm_sweep.should_arm(
        _pr(12, checks=(("playwright-tests", "SUCCESS"),)), REQUIRED, NOW_TS)
    assert decision is False and "has not reported" in why, why


def test_every_required_check_must_be_green():
    """philanthropy/main requires BOTH test and test-postgres -- one green is not enough."""
    pr = _pr(13, checks=(("test", "SUCCESS"), ("test-postgres", "FAILURE")))
    decision, why = pr_arm_sweep.should_arm(pr, ["test", "test-postgres"], NOW_TS)
    assert decision is False and "test-postgres" in why, why


def test_no_required_checks_never_arms():
    """Auto-merge on an ungated branch would merge on nothing at all -- that needs a human."""
    decision, why = pr_arm_sweep.should_arm(_pr(14), [], NOW_TS)
    assert decision is False and "no required checks" in why, why


def test_conflicting_pr_needs_a_rebase_not_an_arm():
    decision, why = pr_arm_sweep.should_arm(
        _pr(15, mergeable="CONFLICTING"), REQUIRED, NOW_TS)
    assert decision is False and "conflicting" in why, why


def test_unknown_mergeable_still_arms():
    """gh reports mergeable UNKNOWN while GitHub is still computing it (fleet-kit#973 did).
    That is not a conflict, and refusing to arm on it would reintroduce the exact stall."""
    decision, why = pr_arm_sweep.should_arm(_pr(973, mergeable="UNKNOWN"), REQUIRED, NOW_TS)
    assert decision is True, why


def test_commit_status_shape_is_matched_too():
    """A plain commit status carries context/state, not name/conclusion -- a required check
    reported that way must still read as green or a green PR never gets armed."""
    pr = _pr(16, checks=())
    pr["statusCheckRollup"] = [{"context": "selftest", "state": "SUCCESS"}]
    decision, why = pr_arm_sweep.should_arm(pr, REQUIRED, NOW_TS)
    assert decision is True, why


def test_rerun_green_outvotes_the_earlier_red():
    """dumbledore re-fires false-red CI. The re-run's conclusion is the live one; keeping the
    stale FAILURE would leave the PR unarmed forever after any flake."""
    pr = _pr(17, checks=())
    pr["statusCheckRollup"] = [
        {"name": "selftest", "status": "COMPLETED", "conclusion": "FAILURE",
         "completedAt": "2026-09-12T10:00:00Z"},
        {"name": "selftest", "status": "COMPLETED", "conclusion": "SUCCESS",
         "completedAt": "2026-09-12T11:00:00Z"},
    ]
    decision, why = pr_arm_sweep.should_arm(pr, REQUIRED, NOW_TS)
    assert decision is True, why


def test_closed_pr_is_left_alone():
    decision, why = pr_arm_sweep.should_arm(_pr(18, state="MERGED"), REQUIRED, NOW_TS)
    assert decision is False and why == "not open", why


def test_sweep_reports_every_skip_with_a_reason():
    """The log has to say why a PR was passed over, or the next pass re-investigates it."""
    prs = [_pr(966), _pr(5484, armed=True), _pr(9, labels=("fleet:hold",))]
    result = pr_arm_sweep.sweep(prs, REQUIRED, NOW_TS)
    assert result["arm"] == [966], result
    assert {s["number"] for s in result["skipped"]} == {5484, 9}, result
    assert all(s["why"] for s in result["skipped"]), result


def test_malformed_created_at_does_not_crash_the_sweep():
    pr = _pr(19)
    pr["createdAt"] = "not-a-date"
    decision, why = pr_arm_sweep.should_arm(pr, REQUIRED, NOW_TS)
    assert decision is False and "0m old" in why, why



def test_red_main_arms_only_the_fix():
    """Paperclip 2 #4: while main's deploy gate is red, a green PR is held; the fix-labeled one arms."""
    plain, fix = _pr(30), _pr(31, labels=("fix:main-red",))
    decision, why = pr_arm_sweep.should_arm(plain, REQUIRED, NOW_TS, main_is_red=True)
    assert decision is False and "main is red" in why, why
    assert pr_arm_sweep.should_arm(fix, REQUIRED, NOW_TS, main_is_red=True)[0] is True
    assert pr_arm_sweep.sweep([plain, fix], REQUIRED, NOW_TS, main_is_red=True)["arm"] == [31]
    assert pr_arm_sweep.sweep([plain, fix], REQUIRED, NOW_TS)["arm"] == [30, 31]


def test_main_is_red_reads_the_open_red_item_not_a_rehearsal():
    red = {"title": "the-fixer: main's deploy test gate is red at 5f64cf4404"}
    assert pr_arm_sweep.is_main_red([red]) is True
    assert pr_arm_sweep.is_main_red([{"title": red["title"] + " (rehearsal)"}]) is False
    assert pr_arm_sweep.is_main_red([]) is False


def test_an_unreadable_red_state_holds_nothing():
    real = pr_arm_sweep._gh
    pr_arm_sweep._gh = lambda args, repo: (_ for _ in ()).throw(RuntimeError("gh down"))
    try:
        assert pr_arm_sweep.main_red("o/r") is False
    finally:
        pr_arm_sweep._gh = real

# Red main, the fix queued 13th behind PRs that each run against the red base (philanthropy
# 2026-11-03, #15303). The shapes are that day's: required test/test-postgres/lint/hq-crawl-gate/
# fleet-code-review, hq-crawl-gate SKIPPED, the review a commit status.
PROD_REQUIRED = ["test", "test-postgres", "lint", "hq-crawl-gate", "fleet-code-review"]


def _fix(number=15303, *, labels=("bug", "fix:main-red"), review="SUCCESS", test="SUCCESS",
         draft=False):
    pr = _pr(number, labels=labels, draft=draft,
             checks=(("test", test), ("test-postgres", "SUCCESS"), ("lint", "SUCCESS"),
                     ("hq-crawl-gate", "SKIPPED")))
    if review:
        pr["statusCheckRollup"].append({"context": "fleet-code-review", "state": review})
    pr["id"], pr["headRefOid"] = f"PR_{number}", "2de6f07"
    return pr


QUEUE = {15295: 1, 15290: 10, 15303: 13}


def test_red_main_jumps_the_green_reviewed_fix_to_the_front():
    go, why = pr_arm_sweep.should_jump(_fix(), PROD_REQUIRED, True, QUEUE)
    assert go is True, why
    out = pr_arm_sweep.sweep([_fix(15290, labels=()), _fix()], PROD_REQUIRED, NOW_TS,
                             main_is_red=True, queue=QUEUE)
    assert [j["number"] for j in out["jump"]] == [15303], out["jump"]
    assert out["jump"][0]["head"] == "2de6f07" and out["jump"][0]["id"] == "PR_15303"


def test_green_main_jumps_nothing():
    assert pr_arm_sweep.should_jump(_fix(), PROD_REQUIRED, False, QUEUE)[0] is False
    assert pr_arm_sweep.sweep([_fix()], PROD_REQUIRED, NOW_TS, queue=QUEUE)["jump"] == []


def test_only_the_fix_label_jumps():
    go, why = pr_arm_sweep.should_jump(_fix(15290, labels=("bug",)), PROD_REQUIRED, True, QUEUE)
    assert go is False and "not the fix" in why, why


def test_fix_with_red_or_pending_ci_does_not_jump():
    for state in ("FAILURE", "IN_PROGRESS"):
        go, why = pr_arm_sweep.should_jump(_fix(test=state), PROD_REQUIRED, True, QUEUE)
        assert go is False and "'test'" in why, (state, why)


def test_fix_without_a_passed_review_does_not_jump():
    for review in (None, "PENDING", "FAILURE"):
        go, why = pr_arm_sweep.should_jump(_fix(review=review), PROD_REQUIRED, True, QUEUE)
        assert go is False, (review, why)


def test_all_skipped_ci_is_not_green():
    pr = _fix()
    for c in pr["statusCheckRollup"]:
        if c.get("name"):
            c["conclusion"] = "SKIPPED"
    go, why = pr_arm_sweep.should_jump(pr, PROD_REQUIRED, True, QUEUE)
    assert go is False and "all skipped" in why, why


def test_fix_already_first_or_no_queue_or_draft_does_not_jump():
    assert pr_arm_sweep.should_jump(_fix(), PROD_REQUIRED, True, {15303: 1})[0] is False
    assert pr_arm_sweep.should_jump(_fix(), PROD_REQUIRED, True, None)[0] is False
    assert pr_arm_sweep.should_jump(_fix(draft=True), PROD_REQUIRED, True, QUEUE)[0] is False
    # not in the queue at all is fine: enqueue with jump puts it first
    assert pr_arm_sweep.should_jump(_fix(), PROD_REQUIRED, True, {15295: 1})[0] is True


class _FakeGql:
    """Records mutations; an enqueue of a PR already queued answers with its old position."""
    def __init__(self, queued_at):
        self.queued_at, self.calls = queued_at, []

    def __call__(self, query, **v):
        self.calls.append(("dequeue" if "dequeuePullRequest" in query else "enqueue", v))
        if "dequeuePullRequest" in query:
            self.queued_at = None
            return {"data": {"dequeuePullRequest": {}}}
        assert "jump:true" in query and v.get("head") == "2de6f07", (query, v)
        pos = self.queued_at or 1
        self.queued_at = pos
        return {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": pos}}}}


def test_jump_requeues_a_fix_that_kept_its_place():
    fake = _FakeGql(queued_at=13)
    assert pr_arm_sweep.jump_to_front("PR_15303", "2de6f07", gql=fake) == 1
    assert [c[0] for c in fake.calls] == ["enqueue", "dequeue", "enqueue"], fake.calls


def test_jump_of_an_unqueued_fix_is_one_enqueue():
    fake = _FakeGql(queued_at=None)
    assert pr_arm_sweep.jump_to_front("PR_15303", "2de6f07", gql=fake) == 1
    assert [c[0] for c in fake.calls] == ["enqueue"], fake.calls


def test_queue_positions_reads_the_queue_and_none_without_one():
    data = {"data": {"repository": {"mergeQueue": {"entries": {"nodes": [
        {"position": 1, "pullRequest": {"number": 15295}},
        {"position": 13, "pullRequest": {"number": 15303}}]}}}}}
    assert pr_arm_sweep.queue_positions("o/r", "main", gql=lambda q, **v: data) == {15295: 1, 15303: 13}
    none = {"data": {"repository": {"mergeQueue": None}}}
    assert pr_arm_sweep.queue_positions("o/r", "main", gql=lambda q, **v: none) is None

# Red QUEUE, green main (philanthropy 2026-11-03 3 AM): every full merge-group run failed `test` on
# one CI-gate test since #15272; the fix #15318 carries fix:main-red. Newest group first.
QUEUE_RED = [{"test"}, {"lint"}, {"test"}]


def test_a_check_failing_two_of_three_merge_groups_makes_the_queue_red():
    assert pr_arm_sweep.queue_red_checks(QUEUE_RED, PROD_REQUIRED) == ["test"]
    # one PR's own failure (a lone lint, a lone test) is not the queue's red
    assert pr_arm_sweep.queue_red_checks([{"lint"}, {"test"}, set()], PROD_REQUIRED) == []
    # a red that is not a required check, or older than the last three groups, does not count
    assert pr_arm_sweep.queue_red_checks([{"pip-audit"}, {"pip-audit"}], PROD_REQUIRED) == []
    assert pr_arm_sweep.queue_red_checks([set(), set(), {"test"}, {"test"}], PROD_REQUIRED) == []


def test_red_queue_jumps_the_fix_but_holds_no_other_arm():
    plain = _pr(15290, checks=[(c, "SUCCESS") for c in PROD_REQUIRED])
    out = pr_arm_sweep.sweep([plain, _fix(15318)], PROD_REQUIRED, NOW_TS, queue_is_red=True,
                             queue={15289: 1, 15318: 16})  # 15290 not queued yet
    assert [j["number"] for j in out["jump"]] == [15318], out["jump"]
    assert out["arm"] == [15290], out  # a red queue is not a red main: other PRs still arm
    green = pr_arm_sweep.sweep([_fix(15318)], PROD_REQUIRED, NOW_TS, queue={15318: 16})
    assert green["jump"] == []


def test_merge_group_failures_reads_failed_jobs_per_group_newest_first():
    runs = {"workflow_runs": [
        {"id": 5, "head_sha": "e", "conclusion": "failure"},
        {"id": 6, "head_sha": "x", "conclusion": "cancelled"},  # 2026-11-03: rebuilds cancel
        {"id": 4, "head_sha": "d", "conclusion": "failure"},
        {"id": 3, "head_sha": "c", "conclusion": "success"},
        {"id": 2, "head_sha": "b", "conclusion": "failure"}]}
    jobs = {5: [{"name": "test", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}],
            4: [{"name": "lint", "conclusion": "failure"}]}
    seen = []

    def api(path):
        seen.append(path)
        if path.endswith("per_page=100"):
            return {"jobs": jobs[int(path.split("/runs/")[1].split("/")[0])]}
        return runs
    assert pr_arm_sweep.merge_group_failures("o/r", api=api) == [{"test"}, {"lint"}, set()]
    assert not any("/runs/2/" in s for s in seen), seen  # never reads past the last three groups
    boom = lambda path: (_ for _ in ()).throw(RuntimeError("gh down"))  # noqa: E731
    assert pr_arm_sweep.merge_group_failures("o/r", api=boom) == []

# Armed, green, reviewed, never queued (philanthropy 2026-11-03 3:30 AM: #15310 armed by
# github-actions after the arm PAT was rejected; GitHub never queued it, nor #15306/#15322/#15099).
def _armed(number=15310, *, armed_min_ago=35, **kw):
    pr = _fix(number, labels=("bug",), **kw)
    at = (NOW - timedelta(minutes=armed_min_ago)).isoformat().replace("+00:00", "Z")
    pr["autoMergeRequest"] = {"enabledAt": at, "enabledBy": {"login": "app/github-actions"}}
    return pr


LIVE_QUEUE = {15289: 1, 15290: 8}


def test_armed_green_reviewed_pr_the_queue_never_took_is_enqueued():
    go, why = pr_arm_sweep.should_enqueue(_armed(), PROD_REQUIRED, LIVE_QUEUE, NOW_TS)
    assert go is True, why
    out = pr_arm_sweep.sweep([_armed()], PROD_REQUIRED, NOW_TS, queue=LIVE_QUEUE)
    assert [(e["number"], e["head"]) for e in out["enqueue"]] == [(15310, "2de6f07")], out
    assert out["jump"] == [] and out["arm"] == [], out  # enqueued at the back, never jumped


def test_enqueue_never_takes_a_pr_that_is_not_ready_or_already_queued():
    cases = {
        "queued": (_armed(15290), LIVE_QUEUE),
        "unarmed": (_fix(15311, labels=()), LIVE_QUEUE),
        "red test": (_armed(test="FAILURE"), LIVE_QUEUE),
        "no review": (_armed(review=None), LIVE_QUEUE),
        "just armed": (_armed(armed_min_ago=3), LIVE_QUEUE),
        "draft": (_armed(draft=True), LIVE_QUEUE),
        "no queue": (_armed(), None),
    }
    for name, (pr, q) in cases.items():
        go, why = pr_arm_sweep.should_enqueue(pr, PROD_REQUIRED, q, NOW_TS)
        assert go is False, (name, why)
    held = _armed()
    held["labels"].append({"name": "do-not-merge"})
    assert pr_arm_sweep.should_enqueue(held, PROD_REQUIRED, LIVE_QUEUE, NOW_TS)[0] is False
    assert pr_arm_sweep.should_enqueue(_armed(), PROD_REQUIRED, LIVE_QUEUE, NOW_TS,
                                       main_is_red=True)[0] is False


def test_enqueue_at_back_pins_the_head_and_never_jumps():
    seen = []

    def gql(query, **v):
        seen.append((query, v))
        return {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 16}}}}
    assert pr_arm_sweep.enqueue_at_back("PR_15310", "87755fe", gql=gql) == 16
    assert "jump" not in seen[0][0] and seen[0][1] == {"id": "PR_15310", "head": "87755fe"}, seen

# Launch day (desk 11-03): #15319 (PHIA-2495, launch) green and reviewed, 26th of 30.
def _launch_pr(number=15319, *, branch="jobs/phia-2495-signup-in-post", **kw):
    pr = _fix(number, labels=(), **kw)
    pr["title"], pr["headRefName"] = "Bug: PHIA-2495 sign up inside the post-a-job flow", branch
    return pr


LAUNCH = {"PHIA-2495", "PHIA-2362"}
LQ = {15299: 1, 15000: 2, 15296: 3, 15290: 4, 15319: 26}


def _pick(prs, *, queue=LQ, spec="blocked", red=False, last=0.0):
    return pr_arm_sweep.pick_launch_jump(prs, PROD_REQUIRED, queue, LAUNCH, spec, red, last, NOW_TS)


def test_launch_pr_jumps_once_green_and_reviewed():
    pr, why = _pick([_fix(15290, labels=()), _launch_pr()])
    assert pr and pr["number"] == 15319 and "PHIA-2495" in why, why
    assert pr_arm_sweep.linked_tasks(_launch_pr(branch="x")) == {"PHIA-2495"}
    assert pr_arm_sweep.linked_tasks({"title": "t", "headRefName": "jobs/phia-2495-x"}) == {"PHIA-2495"}


def test_launch_jump_limits():
    assert _pick([_launch_pr(review=None)])[0] is None             # not reviewed
    assert _pick([_launch_pr(test="FAILURE")])[0] is None          # not green
    assert _pick([_launch_pr(15290)], queue={15290: 3})[0] is None  # already top 3
    no_label = _launch_pr()
    no_label["title"], no_label["headRefName"] = "Bug: PHIA-9 other", "b"
    assert _pick([no_label])[0] is None                            # task not labelled launch
    assert _pick([_launch_pr()], last=NOW_TS - 10 * 60)[0] is None  # one per 30 minutes
    assert _pick([_launch_pr()], last=NOW_TS - 31 * 60)[0] is not None


def test_main_red_and_pending_fix_win_over_launch():
    assert "red" in _pick([_launch_pr()], red=True)[1]
    pr, why = _pick([_launch_pr(), _fix(15400)])                    # an open fix:main-red PR
    assert pr is None and "#15400" in why, why


def test_launch_rule_is_off_once_the_spec_closes():
    for spec in ("done", "cancelled", None):
        pr, why = _pick([_launch_pr()], spec=spec)
        assert pr is None and "rule off" in why, (spec, why)


def test_launch_file_read_and_jump_log():
    import json as _json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "launch_tasks.json"
        f.write_text(_json.dumps({"at": NOW_TS - 60, "launch": ["PHIA-2495"], "specs": {"PHIA-2501": "blocked"}}))
        assert pr_arm_sweep.read_launch(str(f), NOW_TS) == ({"PHIA-2495"}, "blocked", "ok")
        assert pr_arm_sweep.read_launch(str(f), NOW_TS + 3600)[2] == "launch file is stale"
        assert pr_arm_sweep.read_launch(str(Path(d) / "none"), NOW_TS)[0] == set()
        log = Path(d) / "launch_jumps.jsonl"
        assert pr_arm_sweep.last_launch_jump(str(log)) == 0.0
        log.write_text(_json.dumps({"ts": 5.0, "pr": 1}) + "\n" + _json.dumps({"ts": 9.0, "pr": 2}) + "\n")
        assert pr_arm_sweep.last_launch_jump(str(log)) == 9.0

# 2026-11-03 7:47 AM: #15337 (PHIA-2492, launch) failed `lint` in its own merge group at 7:07, the
# queue threw it out at 7:20, and the sweep jumped the same head to the front again.
def test_launch_never_rejumps_a_head_the_queue_failed_and_threw_out():
    failed = {15319: NOW_TS - 40 * 60}                                  # its own group failed 40m ago
    pr, why = _pick_failed([_launch_pr()], {15290: 1}, failed)          # not in the queue any more
    assert pr is None, why
    assert _pick_failed([_launch_pr()], LQ, failed)[0] is not None      # still queued: a PR ahead failed it
    old = {15319: NOW_TS - 30 * 3600}                                  # the head checks finished 26h ago
    assert _pick_failed([_launch_pr()], {15290: 1}, old)[0] is not None  # failed before this head's checks


def _pick_failed(prs, queue, failed):
    return pr_arm_sweep.pick_launch_jump(prs, PROD_REQUIRED, queue, LAUNCH, "blocked", False, 0.0, NOW_TS,
                                         failed)


def test_queue_failed_prs_reads_each_prs_own_failed_group():
    runs = {"workflow_runs": [
        {"head_branch": "gh-readonly-queue/main/pr-15337-107f3120", "conclusion": "failure",
         "created_at": "2026-11-03T13:47:54Z"},
        {"head_branch": "gh-readonly-queue/main/pr-15337-a52ebfce", "conclusion": "failure",
         "created_at": "2026-11-03T13:07:39Z"},
        {"head_branch": "gh-readonly-queue/main/pr-15313-2cd4bed3", "conclusion": "success",
         "created_at": "2026-11-03T13:33:12Z"},
        {"head_branch": "gh-readonly-queue/main/pr-15000-107f3120", "conclusion": "cancelled",
         "created_at": "2026-11-03T13:20:50Z"}]}
    got = pr_arm_sweep.queue_failed_prs("o/r", api=lambda path: runs)
    assert got == {15337: datetime(2026, 11, 3, 13, 47, 54, tzinfo=timezone.utc).timestamp()}, got
    boom = lambda path: (_ for _ in ()).throw(RuntimeError("gh down"))  # noqa: E731
    assert pr_arm_sweep.queue_failed_prs("o/r", api=boom) == {}


# 2026-11-03: on lucky the cron runs pr_arm_sweep.sh outside any git checkout. It called
# arm_pr_auto_merge without the repo, so gh had no -R and fell back to the cwd's git remote:
# "fatal: not a git repository" on every arm since 10-28 (251 failed arms). Run the real shell
# from a non-git dir with a fake gh that, like the real one, needs -R there.
def test_sweep_shell_arms_with_repo_outside_a_checkout():
    import os, shutil, subprocess, tempfile
    src = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        kit = tmp / "kit" / "scripts"
        kit.mkdir(parents=True)
        for f in ("pr_arm_sweep.sh", "merge_arm.sh"):
            shutil.copy(src / f, kit / f)
        (kit / "pr_arm_sweep.py").write_text('print(\'{"arm": [42]}\')\n')
        bindir = tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("""#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$*" in
  "pr view"*title,body*) echo false ;;
  "pr view"*headRefOid*) echo abc123 ;;
  api*statuses*) echo success ;;
  "pr merge"*) [[ " $* " == *" -R "* ]] && exit 0; echo "fatal: not a git repository" >&2; exit 1 ;;
  "repo view"*) echo "fatal: not a git repository" >&2; exit 1 ;;
esac
""")
        gh.chmod(0o755)
        work = tmp / "nogit"
        work.mkdir()
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", GH_LOG=str(tmp / "gh.log"),
                   FLEET_PR_ARM_REPOS="o/r", GIT_CEILING_DIRECTORIES=str(tmp))
        out = subprocess.run(["bash", str(kit / "pr_arm_sweep.sh")], cwd=work, env=env,
                             capture_output=True, text=True, timeout=60).stdout
        assert "o/r: armed auto-merge on #42" in out, out
        merges = [l for l in (tmp / "gh.log").read_text().splitlines() if l.startswith("pr merge")]
        assert merges and all("-R o/r" in l for l in merges), merges


# 2026-11-03 8:47 AM: the fixed arm step re-armed #15093 and #15000, heads the queue had failed.
def test_sweep_never_rearms_or_enqueues_a_head_the_queue_failed():
    pr = _pr(15093, age_h=2.0)                                       # head checks done 2h ago
    failed = {15093: NOW_TS - 30 * 60}                               # own queue group failed 30m ago
    out = pr_arm_sweep.sweep([pr], REQUIRED, NOW_TS, queue={}, queue_failed=failed)
    assert out["arm"] == [] and out["enqueue"] == [] and out["jump"] == [], out
    assert "failed this head" in out["skipped"][0]["why"], out
    stale = {15093: NOW_TS - 5 * 3600}                               # failed before this head's checks
    assert pr_arm_sweep.sweep([pr], REQUIRED, NOW_TS, queue={}, queue_failed=stale)["arm"] == [15093]
    queued = pr_arm_sweep.sweep([pr], REQUIRED, NOW_TS, queue={15093: 3}, queue_failed=failed)
    assert queued["skipped"][0]["why"] == "already in the merge queue", queued  # a PR ahead may have failed it


def test_sweep_does_not_rearm_a_pr_already_in_the_merge_queue():
    pr = _pr(15302, age_h=3.0)                                       # green, no autoMergeRequest
    assert pr_arm_sweep.sweep([pr], REQUIRED, NOW_TS, queue={})["arm"] == [15302]
    out = pr_arm_sweep.sweep([pr], REQUIRED, NOW_TS, queue={15302: 3})
    assert out["arm"] == [] and out["skipped"][0]["why"] == "already in the merge queue", out


for _name, _fn in sorted((n, f) for n, f in list(globals().items()) if n.startswith("test_")):
    check(_name, _fn)

for name, err in fail:
    print(f"FAIL {name}: {err}")
print(f"pr_arm_sweep: {len(ok)} passed, {len(fail)} failed")
raise SystemExit(1 if fail else 0)
