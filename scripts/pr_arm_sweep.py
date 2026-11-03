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
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _not_green_and_reviewed(pr: dict, required: list[str]) -> str | None:
    """None when the PR's current head passed every required check (one really ran green, not all
    skipped) and its code review is success; else the reason it is not."""
    if not required:
        return "branch has no required checks"
    states = {c: _check_state(pr, c) for c in required}
    for context, state in states.items():
        if state not in _PASSED:
            return f"required check {context!r} is {state or 'not reported'} on the head"
    if _check_state(pr, REVIEW_CHECK) != "SUCCESS":
        return f"code review ({REVIEW_CHECK}) has not passed on the head"
    if not any(s == "SUCCESS" for c, s in states.items() if c != REVIEW_CHECK):
        return "no required CI check ran green on the head (all skipped)"
    return None


# Armed but never queued (philanthropy 2026-11-03 3:30 AM: #15310 green, reviewed and armed, but
# the arm came from GITHUB_TOKEN after the arm PAT was rejected, and GitHub never put a PR armed
# that way into the merge queue -- #15306, #15322 and #15099 the same, an hour and more). GitHub
# normally queues an armed PR within a minute of its last check; past this, it is not coming.
ARMED_NOT_QUEUED_MIN = 15.0


def should_enqueue(pr: dict, required: list[str], queue: dict | None, now: float | None = None,
                   main_is_red: bool = False) -> tuple[bool, str]:
    """Pure: is this an armed, green, reviewed PR the merge queue never picked up? Enqueue it
    (at the back, no jump). Same holds as arming: draft, hold label, conflict, red main."""
    now = time.time() if now is None else now
    if queue is None:
        return False, "branch has no merge queue"
    if pr.get("number") in queue:
        return False, "already in the merge queue"
    armed = pr.get("autoMergeRequest")
    if not armed:
        return False, "not armed"
    if (pr.get("state") or "OPEN").upper() != "OPEN" or pr.get("isDraft"):
        return False, "not an open, ready PR"
    if _label_names(pr.get("labels")) & HOLD_LABELS:
        return False, "hold label"
    if main_is_red and FIX_LABEL not in _label_names(pr.get("labels")):
        return False, f"main is red: only the fix enters the queue (label {FIX_LABEL})"
    if (pr.get("mergeable") or "").upper() != "MERGEABLE":
        return False, f"mergeable is {pr.get('mergeable') or 'unknown'}"
    if not pr.get("id") or not pr.get("headRefOid"):
        return False, "no PR id or head sha to pin the enqueue to"
    age = _age_minutes(armed.get("enabledAt") or "", now)
    if age < ARMED_NOT_QUEUED_MIN:
        return False, f"armed {age:.0f}m ago (< {ARMED_NOT_QUEUED_MIN:.0f}m; GitHub may still queue it)"
    not_ready = _not_green_and_reviewed(pr, required)
    if not_ready:
        return False, not_ready
    return True, f"armed {age:.0f}m, green and reviewed, but not in the merge queue"


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
    not_ready = _not_green_and_reviewed(pr, required)
    if not_ready:
        return False, not_ready
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


# Launch day (desk 11-03): a green, reviewed PR whose PHIA task carries `launch` jumps the queue, so the
# launch work lands before the launch mail (#15319, PHIA-2495, sat 26th of 30). Launch-only: the rule
# is off once the launch spec is done or cancelled. The label is read from a file the box's
# launch_labels cron writes every 10 minutes (this container can not read Paperclip's database).
LAUNCH_SPEC = "PHIA-2501"
SPEC_CLOSED = ("done", "cancelled")
LAUNCH_FILE = os.environ.get("FLEET_LAUNCH_FILE", "/var/log/fleet-kit/launch_tasks.json")
LAUNCH_LOG = os.environ.get("FLEET_LAUNCH_JUMP_LOG", "/var/log/fleet-kit/launch_jumps.jsonl")
LAUNCH_FILE_MAX_AGE_MIN = 30.0
LAUNCH_GAP_MIN = 30.0  # at most one launch jump per this many minutes
LAUNCH_TOP = 3         # a PR already this close to the front is not jumped
_TASK = re.compile(r"(?<![A-Za-z0-9])PHIA-([0-9]{1,6})(?![0-9])", re.I)


def linked_tasks(pr: dict) -> set[str]:
    """The PHIA tasks a PR names in its title or branch (PHIA-2495, jobs/phia-2495-...)."""
    text = f"{pr.get('title') or ''} {pr.get('headRefName') or ''}"
    return {f"PHIA-{n}" for n in _TASK.findall(text)}


def read_launch(path: str, now: float) -> tuple[set[str], str | None, str]:
    """(launch tasks, launch spec status, why). Missing, unreadable or stale reads as no launch."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return set(), None, f"no launch file at {path}"
    if (now - float(d.get("at") or 0)) / 60.0 > LAUNCH_FILE_MAX_AGE_MIN:
        return set(), None, "launch file is stale"
    return set(d.get("launch") or []), (d.get("specs") or {}).get(LAUNCH_SPEC), "ok"


def last_launch_jump(path: str) -> float:
    """Unix time of the last launch jump in the log, 0 when there is none."""
    try:
        lines = [ln for ln in Path(path).read_text().splitlines() if ln.strip()]
        return float(json.loads(lines[-1]).get("ts") or 0) if lines else 0.0
    except (OSError, ValueError, AttributeError):
        return 0.0


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def head_checked_at(pr: dict) -> float:
    """Unix time the PR's current head last finished a check (0 when none has)."""
    return max((_ts(c.get("completedAt")) for c in pr.get("statusCheckRollup") or []), default=0.0)


# A launch PR the queue already threw out (philanthropy 2026-11-03 7:47 AM: #15337 failed `lint` in
# its own merge group at 7:07, the queue removed it at 7:20, and the 7:47 sweep jumped the same head
# to the front again; it failed again and turned the 4 groups behind it red). A failed group of its
# own after its head's checks finished is the queue's verdict on that head: do not jump it again.
_QUEUE_REF = re.compile(r"^gh-readonly-queue/[^/]+/pr-([0-9]+)-")


def queue_failed_prs(repo: str, limit: int = 50, api=None) -> dict[int, float]:
    """{PR number: unix time of its newest failed merge group}; {} on a read error."""
    api = api or _api
    try:
        runs = api(f"repos/{repo}/actions/runs?event=merge_group&status=completed&per_page={limit}")
        out: dict[int, float] = {}
        for r in runs.get("workflow_runs") or []:
            m = _QUEUE_REF.match(r.get("head_branch") or "")
            if m and r.get("conclusion") == "failure":
                n = int(m.group(1))
                out[n] = max(out.get(n, 0.0), _ts(r.get("created_at")))
        return out
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
        return {}


def pick_launch_jump(prs: list[dict], required: list[str], queue: dict | None, launch: set[str],
                     spec_status: str | None, line_red: bool, last_jump: float,
                     now: float, queue_failed: dict | None = None) -> tuple[dict | None, str]:
    """Pure: the one PR to jump for launch now, or None and why not. Main red always wins."""
    if not spec_status or spec_status in SPEC_CLOSED:
        return None, f"launch spec {LAUNCH_SPEC} is {spec_status or 'unknown'}: rule off"
    if line_red:
        return None, "main or the queue is red: the fix goes first"
    fixes = [p.get("number") for p in prs if FIX_LABEL in _label_names(p.get("labels"))]
    if fixes:
        return None, f"a {FIX_LABEL} PR is pending (#{fixes[0]}): the fix goes first"
    if queue is None:
        return None, "branch has no merge queue"
    since = (now - last_jump) / 60.0
    if since < LAUNCH_GAP_MIN:
        return None, f"last launch jump {since:.0f}m ago (< {LAUNCH_GAP_MIN:.0f}m)"
    for pr in sorted(prs, key=lambda p: p.get("number") or 0):
        tasks = sorted(linked_tasks(pr) & launch)
        if not tasks:
            continue
        if (pr.get("state") or "OPEN").upper() != "OPEN" or pr.get("isDraft"):
            continue
        if (pr.get("mergeable") or "").upper() == "CONFLICTING" or not pr.get("id") or not pr.get("headRefOid"):
            continue
        where = queue.get(pr.get("number"))
        if where is not None and where <= LAUNCH_TOP:
            continue
        if where is None and (queue_failed or {}).get(pr.get("number"), 0.0) > head_checked_at(pr):
            continue  # the queue failed this head and threw it out: a jump only fails it again
        if _not_green_and_reviewed(pr, required):
            continue
        return pr, f"launch task {tasks[0]} ({'queued #%d' % where if where else 'not queued'}), green and reviewed"
    return None, "no green, reviewed launch PR outside the top 3"


def sweep(prs: list[dict], required: list[str], now: float | None = None,
          min_age_min: float = MIN_AGE_MIN, main_is_red: bool = False,
          queue: dict | None = None, queue_is_red: bool = False) -> dict:
    arm, skip, jump, enqueue = [], [], [], []
    for pr in prs:
        ok, why = should_arm(pr, required, now, min_age_min, main_is_red)
        (arm if ok else skip).append({"number": pr.get("number"), "why": why})
        # A red queue moves the fix up; it does not hold the other PRs' arming (main red does).
        go, jwhy = should_jump(pr, required, main_is_red or queue_is_red, queue)
        if go:
            jump.append({"number": pr.get("number"), "id": pr.get("id"),
                         "head": pr.get("headRefOid"), "why": jwhy})
            continue
        go, ewhy = should_enqueue(pr, required, queue, now, main_is_red)
        if go:
            enqueue.append({"number": pr.get("number"), "id": pr.get("id"),
                            "head": pr.get("headRefOid"), "why": ewhy})
    return {"arm": [a["number"] for a in arm], "armed_why": arm, "skipped": skip, "jump": jump,
            "enqueue": enqueue}


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
                "number,id,title,headRefName,headRefOid,isDraft,createdAt,autoMergeRequest,labels,mergeable,state,"
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
_ENQUEUE_BACK = ("mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,"
                 "expectedHeadOid:$head}){mergeQueueEntry{position}}}")


def enqueue_at_back(pr_id: str, head: str, gql=_graphql) -> int | None:
    """Enqueue (no jump), pinned to the green head. Returns the queue position."""
    d = gql(_ENQUEUE_BACK, id=pr_id, head=head)
    return (((d.get("data") or {}).get("enqueuePullRequest") or {})
            .get("mergeQueueEntry") or {}).get("position")


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
    ap.add_argument("--apply-enqueues", action="store_true",
                    help="enqueue each PR in `enqueue` (armed, green, reviewed, never queued)")
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
    queue = queue_positions(a.repo, a.branch)
    result = sweep(prs, required, min_age_min=a.min_age_min,
                   main_is_red=red, queue=queue, queue_is_red=bool(qred))
    if a.apply_jumps:
        for j in result["jump"]:
            try:
                j["position"] = jump_to_front(j["id"], j["head"])
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                j["error"] = str(exc)[:300]
    launch, spec, lwhy = read_launch(LAUNCH_FILE, time.time())
    pick, pwhy = (None, lwhy) if lwhy != "ok" else pick_launch_jump(
        prs, required, queue, launch, spec, bool(red or qred), last_launch_jump(LAUNCH_LOG), time.time(),
        queue_failed_prs(a.repo))
    result["launch_jump"] = {"why": pwhy}
    if pick:
        result["launch_jump"].update(number=pick.get("number"), id=pick.get("id"), head=pick.get("headRefOid"))
    if pick and a.apply_jumps:
        lj = result["launch_jump"]
        try:
            lj["position"] = jump_to_front(pick["id"], pick["headRefOid"])
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            lj["error"] = str(exc)[:300]
        # Log every attempt, not only a confirmed position 1: on 11-03 GitHub answered a stale
        # position after the jump had landed, nothing was logged, and the 30-minute gap never held.
        try:
            with open(LAUNCH_LOG, "a") as f:
                f.write(json.dumps({"ts": time.time(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "pr": pick.get("number"), "reason": pwhy}) + "\n")
        except OSError as exc:
            lj["log_error"] = str(exc)[:300]
    if pick:  # a launch jump already put it in the queue
        result["enqueue"] = [e for e in result["enqueue"] if e["number"] != pick.get("number")]
    if a.apply_enqueues:
        for e in result["enqueue"]:
            try:
                e["position"] = enqueue_at_back(e["id"], e["head"])
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                e["error"] = str(exc)[:300]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
