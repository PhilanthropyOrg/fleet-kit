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


for _name, _fn in sorted((n, f) for n, f in list(globals().items()) if n.startswith("test_")):
    check(_name, _fn)

for name, err in fail:
    print(f"FAIL {name}: {err}")
print(f"pr_arm_sweep: {len(ok)} passed, {len(fail)} failed")
raise SystemExit(1 if fail else 0)
