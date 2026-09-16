#!/usr/bin/env python3
"""pr_arm_sweep -- which open PRs are green, unarmed, and safe to arm for auto-merge?

THE GAP THIS CLOSES. This fleet merges on green gates with no human in the loop by design
(members/minion/minion.md step 9: "arming auto-merge IS finishing the job"). But ARMING is a
step in a charter, so it happens only when an LLM pass reaches the end of its checklist and
remembers. `scripts/worktree_builder.sh` arms deterministically; `run_member.sh` -- the path
every real member pass actually takes -- does not, and neither does any cron. So a pass that
opens a PR and then runs out of budget, or is a member whose charter forbids merging at all
(members/dumbledore/dumbledore.md: "you may not merge"), leaves a green PR that nothing will
ever merge.

MEASURED 2026-09-12, this instance. The gap is concentrated in the KIT's own repo, which is
exactly where the fleet's self-improvement lands:
  * fleet-kit: 3 of 3 open PRs unarmed -- #973 (6h), #966 (26.3h), #964 (26.5h) -- and every
    one of them already green on `selftest`, the only required check that branch has. Nothing
    was blocking them. Nobody had armed them.
  * philanthropy: 13 of 45 open PRs unarmed, BUT this sweep would arm none of them: all 13 are
    waiting on `test`/`test-postgres`, which are backlogged behind a CPU-bound CI queue
    (philanthropy#5611). That is a throughput problem, not an arming one, and this script is
    deliberately silent about it -- it arms green PRs, it does not chase red ones.
  * The cost is not the idle PR, it is the loop it feeds. An unmerged PR cannot autoclose its
    issue, so the issue stays open, so `vp_due.py`/gru keep reading it as unbuilt and keep
    dispatching: 171 redo spawns that day, 18 at philanthropy#5237 alone, whose fix (PR #5484,
    `Part of #5237`) had been open since 13:49. 61 of minion's 127 productive passes in 24h
    ended in "no new PR -- already fixed by an open PR" ($22.82, 767 turns); 191 passes,
    $121.79 and 4,391 turns over 7 days, every one of them rediscovering an open PR.
  * The two oldest of those kit PRs, #966 and #973, are themselves fixes for that same redo
    loop. The fleet had already written the cure twice and left it on the shelf.

So this is a script on cron, not a line in a charter -- the same reason `vp_due.py` exists
("the loop that gets a spec up to par has to be deterministic").

WHAT THIS DELIBERATELY DOES NOT DO. It never merges. It only ARMS, which hands the decision
back to the branch's own required checks -- so nothing merges here that would not have merged
had the authoring pass remembered its own step 9. It skips anything that looks deliberately
parked (a hold label, a draft) and anything younger than MIN_AGE_MIN, so an authoring pass
still in flight is never raced to its own PR.

Pure core (`should_arm`, `sweep`), thin `gh` seam (`collect`), CLI (`main`) -- same split as
vp_due.py and claim_history.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

# A pass that opened a PR 30 minutes ago may still be inside its own step 9. Below this age the
# sweep leaves the PR alone rather than racing the author to it: arming is idempotent, but a
# "someone else armed it" surprise in a member's own transcript is a debugging cost for free.
MIN_AGE_MIN = 30.0

# Labels that mean a human (or a charter) parked this on purpose. `fleet:needs-human-op` is the
# fleet's existing name for "no agent can resolve this" (see scripts/test_needs_human_op.py);
# the other two are the conventional GitHub spellings a human reaches for by hand.
HOLD_LABELS = frozenset({"fleet:needs-human-op", "do-not-merge", "fleet:hold"})


def _age_minutes(created_at: str, now: float) -> float:
    try:
        ts = datetime.fromisoformat((created_at or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts.timestamp()) / 60.0


def _label_names(labels) -> set[str]:
    return {(lab.get("name", "") if isinstance(lab, dict) else str(lab)) for lab in labels or []}


def _check_state(pr: dict, context: str) -> str | None:
    """The conclusion of required check `context` on this PR, or None when it has not reported.

    gh's `statusCheckRollup` mixes two shapes: check runs carry `name` + `conclusion`, plain
    commit statuses carry `context` + `state`. A required check must be matched under either
    spelling or a green PR reads as pending and never gets armed.
    """
    best = None
    for c in pr.get("statusCheckRollup") or []:
        if (c.get("name") or c.get("context")) != context:
            continue
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if not state:
            # a check run that is still QUEUED/IN_PROGRESS has no conclusion yet
            return (c.get("status") or "PENDING").upper()
        # keep the newest reporting run for this context: gh lists re-runs, and an old FAILURE
        # must not outvote the re-run that went green (the false-red case dumbledore re-fires)
        if best is None or (c.get("completedAt") or "") >= best[1]:
            best = (state, c.get("completedAt") or "")
    return best[0] if best else None


def should_arm(pr: dict, required: list[str], now: float | None = None,
               min_age_min: float = MIN_AGE_MIN) -> tuple[bool, str]:
    """Pure: is this PR green, unarmed, and safe to arm? Returns (decision, reason).

    `required` is the branch's own required-check contexts, read from branch protection -- not
    a list this file hardcodes, so a repo that adds or renames a gate needs no change here.
    """
    now = time.time() if now is None else now
    if (pr.get("state") or "OPEN").upper() != "OPEN":
        return False, "not open"
    if pr.get("isDraft"):
        return False, "draft"
    if pr.get("autoMergeRequest"):
        return False, "already armed"
    held = _label_names(pr.get("labels")) & HOLD_LABELS
    if held:
        return False, f"hold label {sorted(held)[0]}"
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "conflicting -- needs a rebase, not an arm"
    age = _age_minutes(pr.get("createdAt") or "", now)
    if age < min_age_min:
        return False, f"only {age:.0f}m old (< {min_age_min:.0f}m; author may still be arming)"
    if not required:
        # No required check means auto-merge would merge on nothing at all. That is a repo
        # configuration a human should see, not a thing to arm into.
        return False, "branch has no required checks -- arming would merge ungated"
    for context in required:
        state = _check_state(pr, context)
        if state is None:
            return False, f"required check {context!r} has not reported"
        if state != "SUCCESS":
            return False, f"required check {context!r} is {state}"
    return True, f"green on {', '.join(required)} and unarmed for {age:.0f}m"


def sweep(prs: list[dict], required: list[str], now: float | None = None,
          min_age_min: float = MIN_AGE_MIN) -> dict:
    arm, skip = [], []
    for pr in prs:
        ok, why = should_arm(pr, required, now, min_age_min)
        (arm if ok else skip).append({"number": pr.get("number"), "why": why})
    return {"arm": [a["number"] for a in arm], "armed_why": arm, "skipped": skip}


def _gh(args: list[str], repo: str) -> object:
    out = subprocess.run(["gh", *args, "--repo", repo], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "[]")


def required_contexts(repo: str, branch: str) -> list[str]:
    out = subprocess.run(
        ["gh", "api", f"/repos/{repo}/branches/{branch}/protection",
         "--jq", ".required_status_checks.contexts"],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return []
    try:
        return list(json.loads(out.stdout or "[]") or [])
    except ValueError:
        return []


def collect(repo: str, limit: int = 100) -> list[dict]:
    return _gh(["pr", "list", "--state", "open", "--limit", str(limit), "--json",
                "number,isDraft,createdAt,autoMergeRequest,labels,mergeable,state,"
                "statusCheckRollup"], repo)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Print the open PRs that are green, unarmed and safe to arm, as JSON. "
                     "Arming itself is pr_arm_sweep.sh's job (it owns merge_arm.sh).")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--min-age-min", type=float, default=MIN_AGE_MIN)
    ap.add_argument("--limit", type=int, default=100)
    a = ap.parse_args(argv)
    try:
        prs = collect(a.repo, a.limit)
    except (RuntimeError, ValueError) as exc:
        print(f"pr_arm_sweep: {exc}", file=sys.stderr)
        return 1
    result = sweep(prs, required_contexts(a.repo, a.branch), min_age_min=a.min_age_min)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
