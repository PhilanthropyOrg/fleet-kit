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
# Red main stops the line (Paperclip 2 change #4, philanthropy PHIA-2420): while the product repo
# has an open "main's deploy test gate is red" item (its deploy.yml files it, and closes it on the
# next green gate), only the fix arms. The product repo's own sweep disarms the rest
# (scripts/ci/main_red_hold.py); without this, this cron re-armed them within 20 minutes.
RED_MAIN_PREFIX = "the-fixer: main's deploy test gate is red at "
FIX_LABEL = "fix:main-red"


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
               min_age_min: float = MIN_AGE_MIN, main_is_red: bool = False) -> tuple[bool, str]:
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
    if main_is_red and FIX_LABEL not in _label_names(pr.get("labels")):
        return False, f"main is red: only the fix arms (label {FIX_LABEL})"
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "conflicting -- needs a rebase, not an arm"
    # A head judge-judy BLOCKed (or gave up on) is never armed: fleet-code-review is not a
    # required check, so an armed blocked PR simply merges (fleet-kit#523, gh#806). Same rule as
    # auto_update_branch.sh's arm loop. Absent means unreviewed -- the norm on kit PRs.
    review = _check_state(pr, "fleet-code-review")
    if review in ("FAILURE", "ERROR"):
        return False, f"judge-judy blocked this head (fleet-code-review {review})"
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


# A check GitHub counts as passed. SKIPPED alone is not "CI ran green": a draft's run skips
# every job (2026-09-29), so a jump also needs one required check that really SUCCEEDED.
_PASSED = ("SUCCESS", "SKIPPED", "NEUTRAL")
REVIEW_CHECK = "fleet-code-review"


def should_jump(pr: dict, required: list[str], main_is_red: bool,
                queue: dict | None) -> tuple[bool, str]:
    """Pure: should this PR jump to the FRONT of the merge queue? Returns (decision, reason).

    The stop-the-line rule's other half (philanthropy 2026-11-03: the fix #15303 sat 13th in a
    one-at-a-time queue behind 12 PRs that each ran against the red main, launch #15290 10th).
    Holding the rest does not move the fix up; only a jump does. So while main is red, the PR
    labeled FIX_LABEL jumps when its current head is green on every required check and its
    code review passed. Never any other PR, and never on a red, pending or unreviewed head.
    `queue` maps PR number -> queue position (1 = front); None means the branch has no queue.
    """
    if not main_is_red:
        return False, "main is not red and the queue is not red"
    if FIX_LABEL not in _label_names(pr.get("labels")):
        return False, f"not the fix (label {FIX_LABEL})"
    if queue is None:
        return False, "branch has no merge queue"
    if (pr.get("state") or "OPEN").upper() != "OPEN" or pr.get("isDraft"):
        return False, "not an open, ready PR"
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "conflicting -- needs a rebase, not a jump"
    if not pr.get("id") or not pr.get("headRefOid"):
        return False, "no PR id or head sha to pin the jump to"
    if queue.get(pr.get("number")) == 1:
        return False, "already first in the merge queue"
    if not required:
        return False, "branch has no required checks"
    states = {c: _check_state(pr, c) for c in required}
    for context, state in states.items():
        if state not in _PASSED:
            return False, f"required check {context!r} is {state or 'not reported'} on the head"
    if _check_state(pr, REVIEW_CHECK) != "SUCCESS":
        return False, f"code review ({REVIEW_CHECK}) has not passed on the head"
    if not any(s == "SUCCESS" for c, s in states.items() if c != REVIEW_CHECK):
        return False, "no required CI check ran green on the head (all skipped)"
    where = queue.get(pr.get("number"))
    return True, f"the line is red and this is the green, reviewed fix ({'queued #%d' % where if where else 'not queued'})"


# A red QUEUE stops the line too (philanthropy 2026-11-03 3 AM: main green, but every full
# merge-group run failed `test` on one CI-gate test since #15272, so nothing landed and each entry
# cost ~52 minutes). A required check that failed in QUEUE_RED_FAILS of the last QUEUE_RED_RUNS
# finished merge groups is the queue's own red; one PR's own failure is a single run, not two.
QUEUE_RED_RUNS = 3
QUEUE_RED_FAILS = 2


def queue_red_checks(groups: list[set], required: list[str], runs: int = QUEUE_RED_RUNS,
                     fails: int = QUEUE_RED_FAILS) -> list[str]:
    """Pure: required checks that failed in `fails`+ of the newest `runs` finished merge groups.

    `groups` is newest first, one set of FAILED job names per merge-group head sha. The review
    is a PR status, never a merge-group job, so it can not make the queue red."""
    recent = groups[:runs]
    return [c for c in required if c != REVIEW_CHECK
            and sum(1 for g in recent if c in g) >= fails]


def sweep(prs: list[dict], required: list[str], now: float | None = None,
          min_age_min: float = MIN_AGE_MIN, main_is_red: bool = False,
          queue: dict | None = None, queue_is_red: bool = False) -> dict:
    arm, skip, jump = [], [], []
    for pr in prs:
        ok, why = should_arm(pr, required, now, min_age_min, main_is_red)
        (arm if ok else skip).append({"number": pr.get("number"), "why": why})
        # A red queue moves the fix up; it does not hold the other PRs' arming (main red does).
        go, jwhy = should_jump(pr, required, main_is_red or queue_is_red, queue)
        if go:
            jump.append({"number": pr.get("number"), "id": pr.get("id"),
                         "head": pr.get("headRefOid"), "why": jwhy})
    return {"arm": [a["number"] for a in arm], "armed_why": arm, "skipped": skip, "jump": jump}


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


def is_main_red(issues) -> bool:
    """Pure: does any open item say main's deploy test gate is red? (rehearsals are not red)"""
    return any((i.get("title") or "").startswith(RED_MAIN_PREFIX)
               and not (i.get("title") or "").endswith("(rehearsal)") for i in issues or [])


def main_red(repo: str) -> bool:
    """Fails open: a read error is "not known red", so a broken read never wedges every merge."""
    try:
        return is_main_red(_gh(["issue", "list", "--state", "open", "--limit", "50", "--search",
                                f'"{RED_MAIN_PREFIX.strip()}" in:title', "--json", "title"], repo))
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return False


def _api(path: str) -> dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "{}")


def merge_group_failures(repo: str, limit: int = 20, api=None) -> list[set]:
    """Newest first: the failed job names of each finished merge-group head. [] on a read error,
    so a broken read never reads as a red queue."""
    api = api or _api
    try:
        runs = api(f"repos/{repo}/actions/runs?event=merge_group&status=completed"
                    f"&per_page={limit}")["workflow_runs"]
        order, failed = [], {}
        for r in runs or []:
            # A cancelled group (a jump, or a rebuild after the queue changed) proves nothing.
            if r.get("conclusion") not in ("failure", "success"):
                continue
            sha = r.get("head_sha")
            if sha not in failed:
                if len(order) == QUEUE_RED_RUNS:
                    break
                order.append(sha)
                failed[sha] = set()
            if r.get("conclusion") == "failure":
                jobs = api(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
                failed[sha] |= {j.get("name") for j in jobs or [] if j.get("conclusion") == "failure"}
        return [failed[s] for s in order]
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
        return []


def collect(repo: str, limit: int = 100) -> list[dict]:
    return _gh(["pr", "list", "--state", "open", "--limit", str(limit), "--json",
                "number,id,headRefOid,isDraft,createdAt,autoMergeRequest,labels,mergeable,state,"
                "statusCheckRollup"], repo)


def _graphql(query: str, **variables) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-f", f"{k}={v}"]
    out = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"graphql failed: {(out.stderr or out.stdout).strip()[:300]}")
    return json.loads(out.stdout or "{}")


def queue_positions(repo: str, branch: str, gql=_graphql) -> dict | None:
    """{PR number: position} for the branch's merge queue; None when it has none or the read fails."""
    owner, name = repo.split("/", 1)
    try:
        data = gql("query($o:String!,$n:String!,$b:String!){repository(owner:$o,name:$n){"
                   "mergeQueue(branch:$b){entries(first:100){nodes{position pullRequest{number}}}}}}",
                   o=owner, n=name, b=branch)
        mq = ((data.get("data") or {}).get("repository") or {}).get("mergeQueue")
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return None
    if mq is None:
        return None
    return {e["pullRequest"]["number"]: e["position"]
            for e in (mq.get("entries") or {}).get("nodes") or [] if e.get("pullRequest")}


_ENQUEUE = ("mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,"
            "jump:true,expectedHeadOid:$head}){mergeQueueEntry{position}}}")
_DEQUEUE = "mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}"


def jump_to_front(pr_id: str, head: str, gql=_graphql) -> int:
    """Enqueue with jump, pinned to the green head. A PR already in the queue keeps its place on a
    plain enqueue, so when the answer is not position 1 it is dequeued and enqueued again with jump.
    Returns the new position; raises RuntimeError when it could not be put first."""
    def enqueue():
        d = gql(_ENQUEUE, id=pr_id, head=head)
        return (((d.get("data") or {}).get("enqueuePullRequest") or {})
                .get("mergeQueueEntry") or {}).get("position")
    try:
        pos = enqueue()
    except RuntimeError:
        pos = None
    if pos == 1:
        return 1
    gql(_DEQUEUE, id=pr_id)
    pos = enqueue()
    if pos != 1:
        raise RuntimeError(f"re-enqueued with jump but queue position is {pos}")
    return pos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Print the open PRs that are green, unarmed and safe to arm, as JSON. "
                     "Arming itself is pr_arm_sweep.sh's job (it owns merge_arm.sh).")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--min-age-min", type=float, default=MIN_AGE_MIN)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--apply-jumps", action="store_true",
                    help="put each PR in `jump` (main red, the green reviewed fix) first in the queue")
    a = ap.parse_args(argv)
    try:
        prs = collect(a.repo, a.limit)
    except (RuntimeError, ValueError) as exc:
        print(f"pr_arm_sweep: {exc}", file=sys.stderr)
        return 1
    red = main_red(a.repo)
    if red:
        print(f"pr_arm_sweep: {a.repo} main is red; only PRs labeled {FIX_LABEL} arm", file=sys.stderr)
    required = required_contexts(a.repo, a.branch)
    qred = queue_red_checks(merge_group_failures(a.repo), required) if not red else []
    if qred:
        print(f"pr_arm_sweep: {a.repo} merge queue is red on {', '.join(qred)}; "
              f"a green, reviewed {FIX_LABEL} PR jumps", file=sys.stderr)
    queue = queue_positions(a.repo, a.branch) if (red or qred) else None
    result = sweep(prs, required, min_age_min=a.min_age_min,
                   main_is_red=red, queue=queue, queue_is_red=bool(qred))
    if a.apply_jumps:
        for j in result["jump"]:
            try:
                j["position"] = jump_to_front(j["id"], j["head"])
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                j["error"] = str(exc)[:300]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
