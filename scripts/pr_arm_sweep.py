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
HOLD_LABELS = frozenset({"fleet:needs-human-op", "do-not-merge", "fleet:hold",
                         # The product repo's own arm workflow (philanthropy
                         # auto-merge-open-prs.yml) held these two; this sweep is now the one
                         # armer there (desk 11-03), so it holds them too. `needs:design-review`
                         # is a human parking a UI PR (gh#3703); `ci:batch-red` is the queue
                         # self-heal's circuit breaker (gh#6374: 194 red batches in a week).
                         "needs:design-review", "ci:batch-red"})
# Red main stops the line (Paperclip 2 change #4, philanthropy PHIA-2420): while the product repo
# has an open "main's deploy test gate is red" item (its deploy.yml files it, and closes it on the
# next green gate), only the fix arms. The product repo's own sweep disarms the rest
# (scripts/ci/main_red_hold.py); without this, this cron re-armed them within 20 minutes.
RED_MAIN_PREFIX = "the-fixer: main's deploy test gate is red at "
FIX_LABEL = "fix:main-red"
# The red item names its failing tests ("FAILED tests/test_x.py::..."). A PR that changes one of
# them is the fix too, labelled or not (philanthropy scripts/ci/main_red_hold.py, same rule).
_FAILING = re.compile(r"(?:FAILED|ERROR)\s+(tests/[^\s:]+\.py)")
# A check GitHub counts as passed. SKIPPED alone is not "CI ran green": a draft's run skips
# every job (2026-09-29), so arming or a jump also needs one required check that really SUCCEEDED.
_PASSED = ("SUCCESS", "SKIPPED", "NEUTRAL")
REVIEW_CHECK = "fleet-code-review"


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
    ran_green = False
    for context in required:
        state = _check_state(pr, context)
        if context == REVIEW_CHECK and state != "SUCCESS":
            # Not reviewed yet (a BLOCK returned above). Arm now: the review is a required check,
            # so GitHub queues the PR only once it passes -- what the product repo's own arm
            # workflow did, so a review landing between sweeps waits on nothing.
            continue
        if state is None:
            return False, f"required check {context!r} has not reported"
        if state not in _PASSED:
            return False, f"required check {context!r} is {state}"
        ran_green = ran_green or (state == "SUCCESS" and context != REVIEW_CHECK)
    # 2026-11-03: `hq-crawl-gate` is SKIPPED on every PR that does not touch HQ, and GitHub counts
    # a skipped required check as passed -- requiring SUCCESS left 14 green PRs unarmed. But a run
    # where every CI check skipped (a draft's) is not green.
    if not ran_green:
        return False, "no required CI check ran green on the head (all skipped)"
    return True, f"green on {', '.join(required)} and unarmed for {age:.0f}m"



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
# CI-cost fixes (desk 11-03: CI money red, $53/day; the queue-speed fix #15370 sat 37th) jump the same way and
# share the gap. The label alone is not enough: the PR's PHIA task must carry `judge-yes`, which the same
# launch_labels file lists (its "judge_yes" key), so nobody can buy a jump with a label.
COST_LABEL = "fix:ci-cost"
_TASK = re.compile(r"(?<![A-Za-z0-9])PHIA-([0-9]{1,6})(?![0-9])", re.I)


def linked_tasks(pr: dict) -> set[str]:
    """The PHIA tasks a PR names in its title or branch (PHIA-2495, jobs/phia-2495-...)."""
    text = f"{pr.get('title') or ''} {pr.get('headRefName') or ''}"
    return {f"PHIA-{n}" for n in _TASK.findall(text)}


def read_launch(path: str, now: float, judge_yes: set | None = None) -> tuple[set[str], str | None, str]:
    """(launch tasks, launch spec status, why). Missing, unreadable or stale reads as no launch.
    A `judge_yes` set passed in is filled with the file's judge-yes tasks (the CI-cost jump's guard)."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return set(), None, f"no launch file at {path}"
    if (now - float(d.get("at") or 0)) / 60.0 > LAUNCH_FILE_MAX_AGE_MIN:
        return set(), None, "launch file is stale"
    if judge_yes is not None:
        judge_yes.update(d.get("judge_yes") or [])
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


# A launch PR the queue already threw out (philanthropy 2026-11-03 7:47 AM: #15337 failed `lint` in
# its own merge group at 7:07, the queue removed it at 7:20, and the 7:47 sweep jumped the same head
# to the front again; it failed again and turned the 4 groups behind it red). A failed group of its
# own after its head's checks finished is the queue's verdict on that head: do not jump it again.
# 11-03 5:28 PM: that verdict was read from the last 50 merge-group runs, so #15364's 2:36 PM failure
# (run 51) was forgotten and the sweep jumped the same head again. The PR's own timeline keeps it:
# the queue removed it for failed_checks and nothing was pushed since. Same shape as
# held_by_manual_dequeue: a failed removal holds, a push (new commit or force-push) frees.
_QUEUE_FAILED = ("query($o:String!,$n:String!){repository(owner:$o,name:$n){pullRequests(states:OPEN,"
                 "first:100,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number timelineItems(last:50,"
                 "itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT,PULL_REQUEST_COMMIT,HEAD_REF_FORCE_PUSHED_EVENT])"
                 "{nodes{__typename ... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}}")


def queue_failed_heads(nodes) -> dict[int, float]:
    """Pure: {PR number: unix time the queue removed its current head for failed checks}, for each PR
    (GraphQL node, timeline oldest first) the queue failed with nothing pushed since."""
    out: dict[int, float] = {}
    for pr in nodes or []:
        failed = 0.0
        for ev in ((pr or {}).get("timelineItems") or {}).get("nodes") or []:
            kind = ev.get("__typename")
            if kind == "RemovedFromMergeQueueEvent":
                if (ev.get("reason") or "").lower() == "failed_checks":
                    failed = _ts(ev.get("createdAt"))
            elif kind in ("PullRequestCommit", "HeadRefForcePushedEvent"):
                failed = 0.0
        if failed and (pr or {}).get("number"):
            out[pr["number"]] = failed
    return out


def queue_failed_prs(repo: str, gql=None) -> dict[int, float]:
    """queue_failed_heads for the repo's open PRs; {} on a read error."""
    owner, name = repo.split("/", 1)
    try:
        data = (gql or _graphql)(_QUEUE_FAILED, o=owner, n=name)
        return queue_failed_heads((((data.get("data") or {}).get("repository") or {})
                                   .get("pullRequests") or {}).get("nodes"))
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
        return {}


def pick_launch_jump(prs: list[dict], required: list[str], queue: dict | None, launch: set[str],
                     spec_status: str | None, line_red: bool, last_jump: float,
                     now: float, queue_failed: dict | None = None,
                     dequeued=None, judge_yes: set | None = None) -> tuple[dict | None, str]:
    """Pure: the one PR to jump for launch or CI cost now, or None and why not. Main red always wins.
    A COST_LABEL PR whose task is in `judge_yes` goes before launch PRs: it makes every later merge cheaper."""
    launch_on = bool(spec_status) and spec_status not in SPEC_CLOSED
    if not launch_on and not judge_yes:
        return None, f"launch spec {LAUNCH_SPEC} is {spec_status or 'unknown'}: rule off"
    if line_red:
        return None, "main or the queue is red: the fix goes first"
    # A pending fix:main-red PR holds nothing on a green line: it jumps only on a red line, which returned above
    # (11-03: green, unqueued #15391 held every launch jump from 11:38 AM; #15377, PHIA-2621, sat 33rd).
    if queue is None:
        return None, "branch has no merge queue"
    since = (now - last_jump) / 60.0
    if since < LAUNCH_GAP_MIN:
        return None, f"last launch jump {since:.0f}m ago (< {LAUNCH_GAP_MIN:.0f}m)"
    def wanted(pr):
        if COST_LABEL in _label_names(pr.get("labels")) and linked_tasks(pr) & (judge_yes or set()):
            return "ci-cost fix", sorted(linked_tasks(pr) & judge_yes)
        return "launch task", sorted(linked_tasks(pr) & launch) if launch_on else []
    for pr in sorted(prs, key=lambda p: (wanted(p)[0] != "ci-cost fix", p.get("number") or 0)):
        kind, tasks = wanted(pr)
        if not tasks:
            continue
        if (pr.get("state") or "OPEN").upper() != "OPEN" or pr.get("isDraft"):
            continue
        if (pr.get("mergeable") or "").upper() == "CONFLICTING" or not pr.get("id") or not pr.get("headRefOid"):
            continue
        where = queue.get(pr.get("number"))
        if where is not None and where <= LAUNCH_TOP:
            continue
        if where is None and pr.get("number") in (queue_failed or {}):
            continue  # the queue failed this head and threw it out: a jump only fails it again
        if _not_green_and_reviewed(pr, required):
            continue
        if dequeued is not None and dequeued(pr.get("number")):
            continue  # a human took it out of the queue: no jump puts it back
        return pr, f"{kind} {tasks[0]} ({'queued #%d' % where if where else 'not queued'}), green and reviewed"
    return None, "no green, reviewed launch or ci-cost PR outside the top 3"


def failing_tests(items) -> set[str]:
    """Pure: the test files the open red items name as failing."""
    return {m for i in items or [] for m in _FAILING.findall(i.get("body") or "")}


def is_fix(pr: dict, failing: set | None, files_of=None) -> bool:
    """The fix for a red main: labelled FIX_LABEL, or it changes a test the red item names.
    `files_of(number)` -> the PR's changed paths; called only when the label is missing."""
    if FIX_LABEL in _label_names(pr.get("labels")):
        return True
    return bool(failing and files_of and failing & set(files_of(pr.get("number")) or ()))


# A human who took a PR OUT of the merge queue is holding it (philanthropy gh#6834: re-arming put
# #6802/#6827 back in line four times with blocking findings open). The queue's own churn is a bot.
QUEUE_BOTS = frozenset({"github-actions[bot]", "github-merge-queue[bot]"})


def held_by_manual_dequeue(events) -> bool:
    """Pure: the PR timeline (oldest first) says a human dequeued it and nothing was pushed since.
    Same rule as philanthropy scripts/ci/dequeue_intent.py: a non-bot removal holds, a push frees.
    One addition: a non-bot RE-ADD frees too. This sweep's own fix jump dequeues and re-enqueues
    under the shared token (11-03 10:07: #15355), which is not a human parking it; and nothing of
    ours re-adds a held PR (arm and enqueue skip it), so a non-bot add is someone putting it back.
    A bot add (auto-merge armed with GITHUB_TOKEN) still frees nothing."""
    held = False
    for ev in events or []:
        name = ev.get("event")
        actor = ev.get("actor")
        login = (actor.get("login") if isinstance(actor, dict) else actor) or ""
        if name == "removed_from_merge_queue":
            if login not in QUEUE_BOTS:
                held = True
        elif name == "added_to_merge_queue":
            if login not in QUEUE_BOTS:
                held = False
        elif name in ("head_ref_force_pushed", "committed"):
            held = False
    return held


def sweep(prs: list[dict], required: list[str], now: float | None = None,
          min_age_min: float = MIN_AGE_MIN, main_is_red: bool = False,
          queue: dict | None = None, queue_is_red: bool = False,
          queue_failed: dict | None = None, failing: set | None = None,
          files_of=None, dequeued=None) -> dict:
    """`dequeued(number)` -> True when a human dequeue still holds the PR; asked only of a PR the
    sweep would otherwise arm, jump or enqueue (one timeline read each)."""
    arm, skip, jump, enqueue, disarm = [], [], [], [], []
    for pr in prs:
        n = pr.get("number")
        queued = (queue or {}).get(n) is not None
        # Red main stops the line (philanthropy main_red_hold.py): every PR that is not the fix is
        # held, and an armed one is disarmed, or GitHub queues it onto the red main anyway.
        held_red = main_is_red and not is_fix(pr, failing, files_of)
        if held_red and pr.get("autoMergeRequest") and not queued:
            disarm.append(n)
        # 2026-11-03: the fixed arm step re-armed #15093 and #15000, heads the queue had already
        # failed and thrown out. Back in line, each fails again and reds the groups behind it.
        # Same verdict pick_launch_jump honours: the queue failed this head and nothing was pushed
        # since (queue_failed_heads), so it needs a new push, not another arm, jump or enqueue.
        if not queued and n in (queue_failed or {}):
            skip.append({"number": n,
                         "why": "the merge queue failed this head: push a fix before it re-enters"})
            continue
        ok, why = should_arm(pr, required, now, min_age_min, held_red)
        if ok and queued:
            # 2026-11-03: a queued PR has no autoMergeRequest, so every run "armed" the same 15
            # queued PRs again (a no-op API call each). In the queue is already armed.
            ok, why = False, "already in the merge queue"
        # A red queue moves the fix up; it does not hold the other PRs' arming (main red does).
        go_j, jwhy = should_jump(pr, required, main_is_red or queue_is_red, queue)
        go_e, ewhy = (False, "") if go_j else should_enqueue(pr, required, queue, now, held_red)
        if (ok or go_j or go_e) and dequeued is not None and dequeued(n):
            skip.append({"number": n, "why": "a human took it out of the merge queue and nothing "
                                             "was pushed since"})
            continue
        (arm if ok else skip).append({"number": n, "why": why})
        if go_j:
            jump.append({"number": n, "id": pr.get("id"), "head": pr.get("headRefOid"), "why": jwhy})
        elif go_e:
            enqueue.append({"number": n, "id": pr.get("id"), "head": pr.get("headRefOid"), "why": ewhy})
    return {"arm": [a["number"] for a in arm], "armed_why": arm, "skipped": skip, "jump": jump,
            "enqueue": enqueue, "disarm": disarm}


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


def red_items(repo: str) -> list[dict]:
    """The open red-gate items (title + body). Fails open: a read error is [] ("not known red"),
    so a broken read never wedges every merge."""
    try:
        got = _gh(["issue", "list", "--state", "open", "--limit", "50", "--search",
                   f'"{RED_MAIN_PREFIX.strip()}" in:title', "--json", "title,body"], repo)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return []
    return [i for i in got or [] if is_main_red([i])]


def main_red(repo: str) -> bool:
    return bool(red_items(repo))


def pr_files(repo: str, number: int) -> list[str]:
    """The PR's changed paths; [] on a read error (then only the label makes it the fix)."""
    try:
        got = _gh(["pr", "view", str(number), "--json", "files"], repo)
        return [f.get("path") for f in (got or {}).get("files") or []]
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return []


def dequeue_held(repo: str, number: int) -> bool:
    """True when a human dequeue still holds the PR. Fails CLOSED (philanthropy gh#6834): an
    unreadable timeline is held, since re-arming a PR a human pulled costs more than one sweep."""
    try:
        out = subprocess.run(["gh", "api", "--paginate",
                              f"repos/{repo}/issues/{number}/timeline?per_page=100"],
                             capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return True
        events, dec, i, txt = [], json.JSONDecoder(), 0, out.stdout
        while i < len(txt):
            while i < len(txt) and txt[i] in " \t\r\n":
                i += 1
            if i >= len(txt):
                break
            page, i = dec.raw_decode(txt, i)
            events.extend(page)
        return held_by_manual_dequeue(events)
    except (ValueError, OSError, subprocess.SubprocessError):
        return True


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


RECHECK_SEC = 8
_DEQUEUE = "mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}"


def jump_to_front(pr_id: str, head: str, gql=_graphql, recheck=None) -> int:
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
    if pos != 1 and recheck is not None:
        # 11-03: every jump answered its OLD place (#15343 "position is 35", merged 12 minutes later):
        # the mutation's answer is stale. The queue itself, read a moment later, is the truth.
        time.sleep(RECHECK_SEC)
        pos = recheck() or pos
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
    ap.add_argument("--queue-failed", action="store_true",
                    help="print only the open PRs the merge queue failed with nothing pushed since, space-separated")
    a = ap.parse_args(argv)
    if a.queue_failed:
        # For the other arm paths (auto_update_branch.sh): one read of the same verdict this sweep honours.
        print(" ".join(str(n) for n in sorted(queue_failed_prs(a.repo))))
        return 0
    try:
        prs = collect(a.repo, a.limit)
    except (RuntimeError, ValueError) as exc:
        print(f"pr_arm_sweep: {exc}", file=sys.stderr)
        return 1
    items = red_items(a.repo)
    red = bool(items)
    if red:
        print(f"pr_arm_sweep: {a.repo} main is red; only the fix arms ({FIX_LABEL}, or it changes a "
              f"failing test); armed others are disarmed", file=sys.stderr)
    required = required_contexts(a.repo, a.branch)
    qred = queue_red_checks(merge_group_failures(a.repo), required) if not red else []
    if qred:
        print(f"pr_arm_sweep: {a.repo} merge queue is red on {', '.join(qred)}; "
              f"a green, reviewed {FIX_LABEL} PR jumps", file=sys.stderr)
    queue = queue_positions(a.repo, a.branch)
    qfailed = queue_failed_prs(a.repo)
    held: dict[int, bool] = {}

    def dequeued(n: int) -> bool:
        if n not in held:
            held[n] = dequeue_held(a.repo, n)
        return held[n]
    result = sweep(prs, required, min_age_min=a.min_age_min,
                   main_is_red=red, queue=queue, queue_is_red=bool(qred), queue_failed=qfailed,
                   failing=failing_tests(items), files_of=lambda n: pr_files(a.repo, n),
                   dequeued=dequeued)
    def at(n):  # re-read one PR's queue position (the mutation's own answer can be stale)
        return lambda: (queue_positions(a.repo, a.branch) or {}).get(n)
    if a.apply_jumps:
        for j in result["jump"]:
            try:
                j["position"] = jump_to_front(j["id"], j["head"], recheck=at(j["number"]))
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                j["error"] = str(exc)[:300]
    judge_yes: set[str] = set()
    launch, spec, lwhy = read_launch(LAUNCH_FILE, time.time(), judge_yes)
    pick, pwhy = (None, lwhy) if lwhy != "ok" else pick_launch_jump(
        prs, required, queue, launch, spec, bool(red or qred), last_launch_jump(LAUNCH_LOG), time.time(),
        qfailed, dequeued, judge_yes)
    result["launch_jump"] = {"why": pwhy}
    if pick:
        result["launch_jump"].update(number=pick.get("number"), id=pick.get("id"), head=pick.get("headRefOid"))
    if pick and a.apply_jumps:
        lj = result["launch_jump"]
        try:
            lj["position"] = jump_to_front(pick["id"], pick["headRefOid"], recheck=at(pick.get("number")))
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
