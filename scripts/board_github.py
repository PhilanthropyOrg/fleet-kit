#!/usr/bin/env python3
"""board_github — the fleet's work queue as GitHub Issues.

Provenance: genericized from nonprofit-atlas's `scripts/fleet/board_github.py` (2026-08-12).
That product previously ran its board on a database on its own production box — meaning the
fleet's brain rode on the exact service it existed to maintain (prod down = fleet blind
precisely when it was needed). Issues invert that: fleet state survives any box, `gh` is
already authenticated everywhere the fleet runs, and any repo gets a working board for free.

CONTRACT:
  file  -> `gh issue create` with labels <prefix>backlog [+ <prefix>lane:<lane>]
           [+ <prefix>priority-<priority>]; evidence lives in the body (labels enumerate,
           bodies explain).
  claim -> add label <prefix>claimed + a "claimed-by: <worker>" comment. NOT atomic — GitHub
           has no test-and-set; single-claimer discipline comes from the caller running ONE
           sequential claim loop (see `worktree_builder.sh`'s claim step), not from this file.
           list_unclaimed() re-reads live state before every claim.
  done  -> `gh issue close --comment`.
  list  -> open issues labeled <prefix>backlog, unclaimed first.

Label prefix is `FLEET_LABEL_PREFIX` (env, default "fleet:") so multiple fleet-kit instances
or an existing labeling scheme don't collide.

All GitHub access goes through the `gh` CLI (already authenticated; no token handling here).
Pure command-BUILDERS are separated from execution so a test can assert the exact argv
without a network call.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
LABEL_BACKLOG = f"{PREFIX}backlog"
LABEL_CLAIMED = f"{PREFIX}claimed"


# --- pure command builders (unit-tested; never executed by tests) -----------------------------

def build_file_cmd(title: str, body: str, lane: str = "", priority: str = "") -> list[str]:
    labels = LABEL_BACKLOG
    if lane:
        labels += f",{PREFIX}lane:{lane}"
    if priority:
        labels += f",{PREFIX}priority-{priority}"
    return ["gh", "issue", "create", "--title", title, "--body", body, "--label", labels]


def build_list_cmd() -> list[str]:
    return [
        "gh", "issue", "list", "--state", "open", "--label", LABEL_BACKLOG,
        "--limit", "200", "--json", "number,title,body,labels",
    ]


def build_view_cmd(number: int) -> list[str]:
    return ["gh", "issue", "view", str(number), "--json", "number,title,body,labels"]


def build_claim_cmds(number: int, worker: str) -> list[list[str]]:
    return [
        ["gh", "issue", "edit", str(number), "--add-label", LABEL_CLAIMED],
        ["gh", "issue", "comment", str(number), "--body", f"claimed-by: {worker}"],
    ]


def build_done_cmd(number: int, note: str) -> list[str]:
    return ["gh", "issue", "close", str(number), "--comment", note or "done"]


def build_release_cmds(number: int, note: str) -> list[list[str]]:
    """Strip the claimed label + say why. The counterpart claim() never had: a build that gets
    killed, times out, or exits non-zero left the item fleet:claimed forever, with no worker
    actually working it -- confirmed live as nonprofit-atlas issue #3044 ("374 of 374 open
    backlog items are fleet:claimed, no code path ever removes it") and independently in this
    kit's own deployment-learnings.md #7. Comment first, label second: same ordering as
    code_review_local.sh's findings-then-status (a state change with no explanation attached
    is worse than the stuck state)."""
    return [
        ["gh", "issue", "comment", str(number), "--body", note or "released: build did not finish"],
        ["gh", "issue", "edit", str(number), "--remove-label", LABEL_CLAIMED],
    ]


def is_claimed(issue: dict) -> bool:
    return any(lb.get("name") == LABEL_CLAIMED for lb in issue.get("labels") or [])


LABEL_HUMAN_BLOCKED = f"{PREFIX}needs-human-op"


def is_human_blocked(issue: dict) -> bool:
    """True when the issue is code-complete and blocked only on a one-time human action
    (a prod credential, an OAuth registration, an --apply run) that no sandboxed builder can
    perform. Ported from nonprofit-atlas's `scripts/fleet/board_github.py` (gh#5870): this
    file forked from that one and never picked up the exclusion, so `fleet:needs-human-op`
    was a silent no-op for every claim made through this copy."""
    return any(lb.get("name") == LABEL_HUMAN_BLOCKED for lb in issue.get("labels") or [])


BLOCKED_BY_RE = re.compile(
    r"(?:blocked by|blocked on|blocks on|depends on)\s*:?\s*\**\s*(?:gh)?#(\d+)", re.I)


def blocked_by_numbers(issue: dict) -> list[int]:
    """Issue numbers this item declares itself blocked by, parsed from a `Blocked by #N` line
    in the body. Ported alongside is_human_blocked (gh#5870) -- see that repo's gh#4996 for why
    a bare `#N` in prose is never enough to count as a declared dependency."""
    return [int(m) for m in BLOCKED_BY_RE.findall(issue.get("body") or "")]


def is_blocked_by_open_issue(issue: dict, open_numbers: set[int]) -> bool:
    """True when any issue named by `Blocked by #N` is still open on this board."""
    return any(n in open_numbers for n in blocked_by_numbers(issue))


def to_board_item(issue: dict) -> dict:
    """The {'id','text','context'} shape every caller consumes."""
    return {
        "id": issue["number"],
        "text": issue.get("title") or "",
        "context": issue.get("body") or "",
    }


# --- execution ---------------------------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


# Same colors/description text as marie.md's own `gh label create` lines for these three, so
# a label created here (e.g. by a filing path that runs before marie's next pass) never drifts
# from what marie would have created.
_PRIORITY_LABEL_META = {
    "high": ("d73a4a", "gru builds this first"),
    "medium": ("e4a72c", "gru builds after high is claimed"),
    "low": ("a2eeef", "gru builds only with spare runway"),
}

# gh#726: vision_link_gate.py's severity escape hatch -- set by hand (marie or judge-judy),
# never auto-detected, and cleared the moment the failure stops. Unconditional, same as
# LABEL_BACKLOG/LABEL_CLAIMED below, so any ensure_labels() call creates it if missing.
LABEL_SEVERITY_LIVE = f"{PREFIX}severity-live"
_SEVERITY_LABEL_META = (
    LABEL_SEVERITY_LIVE, "b60205",
    "an active, ongoing failure -- set only while the failure is still occurring; "
    "removed when it stops",
)


def ensure_labels(priority: str = "") -> None:
    """Idempotent: create the fleet labels this call needs if absent (gh errors on duplicates;
    a failed create against an existing label is fine and ignored)."""
    labels = [
        (LABEL_BACKLOG, "3e694a", "fleet work queue item"),
        (LABEL_CLAIMED, "d4a72c", "claimed by a fleet worker"),
        _SEVERITY_LABEL_META,
        (LABEL_HUMAN_BLOCKED, "b60205",
         "code-complete, blocked on a one-time human action -- excluded from claim selection"),
    ]
    if priority:
        color, desc = _PRIORITY_LABEL_META.get(priority, ("ededed", f"priority: {priority}"))
        labels.append((f"{PREFIX}priority-{priority}", color, desc))
    for name, color, desc in labels:
        _run(["gh", "label", "create", name, "--color", color, "--description", desc])


def file_item(title: str, body: str, lane: str = "", priority: str = "") -> int:
    """Returns the issue NUMBER (>0) on success, 0 on failure -- the open twin's number when
    issue_cluster's dedupe-at-birth turned this filing into a comment on it (2026-09-24)."""
    import issue_cluster
    ensure_labels(priority)
    labels = build_file_cmd(title, body, lane, priority)[-1].split(",")
    res = issue_cluster.file_issue(title, body, labels, who=os.environ.get("FLEET_MEMBER", ""))
    if not res.get("ok"):
        print(f"board_github: file FAILED: {res.get('error', '')[:300]}", file=sys.stderr)
        return 0
    if res["action"] == "commented":
        print(f"board_github: deduped onto open #{res['number']} (commented, not filed)")
        return int(res["number"])
    out = res.get("url") or ""
    print(out)  # the issue URL — callers log it as the durable id
    m = re.search(r"/issues/(\d+)\s*$", out)
    return int(m.group(1)) if m else 0


def list_unclaimed() -> list[dict]:
    """What a builder may pick up: open, unclaimed, not human-blocked, and not waiting on
    another open board item (gh#5870)."""
    rc, out = _run(build_list_cmd())
    if rc != 0:
        print(f"board_github: list FAILED: {out[:300]}", file=sys.stderr)
        return []
    try:
        issues = json.loads(out)
    except json.JSONDecodeError:
        return []
    open_numbers = {i["number"] for i in issues if i.get("number") is not None}
    return [to_board_item(i) for i in issues
            if not is_claimed(i) and not is_human_blocked(i)
            and not is_blocked_by_open_issue(i, open_numbers)]


def release_item(number: int, note: str) -> bool:
    """Undo a claim on the failure path. Returns True iff both steps succeeded."""
    ok = True
    for cmd in build_release_cmds(number, note):
        rc, out = _run(cmd)
        if rc != 0:
            print(f"board_github: release #{number} step FAILED: {out[:200]}", file=sys.stderr)
            ok = False
    return ok


def claim_next_n(worker: str, n: int) -> list[dict]:
    """Sequential claim of up to n unclaimed items — stops the moment the queue runs dry
    rather than padding the result (a short queue is the real state, not an error)."""
    claimed: list[dict] = []
    for item in list_unclaimed():
        if len(claimed) >= max(0, n):
            break
        ok = True
        for cmd in build_claim_cmds(item["id"], worker):
            rc, out = _run(cmd)
            if rc != 0:
                print(f"board_github: claim #{item['id']} step FAILED: {out[:200]}", file=sys.stderr)
                ok = False
                break
        if ok:
            claimed.append(item)
    return claimed


def claim_item(worker: str, number: int, run=None) -> list[dict]:
    """fk#1202: claim ONE named issue, claimed already or not, and return it in the same
    [{id,text,context}] shape as claim_next_n. gru pre-claims an item before dispatching a
    fold build; claim_next_n then skipped it (it only picks unclaimed) and built a different
    issue onto the fold target's branch. Idempotent: re-adding the label is a no-op for gh."""
    run = run or _run
    rc, out = run(build_view_cmd(number))
    if rc != 0:
        print(f"board_github: view #{number} FAILED: {out[:200]}", file=sys.stderr)
        return []
    try:
        issue = json.loads(out)
    except json.JSONDecodeError:
        print(f"board_github: view #{number} returned non-JSON: {out[:200]}", file=sys.stderr)
        return []
    for cmd in build_claim_cmds(number, worker):
        rc, out = run(cmd)
        if rc != 0:
            print(f"board_github: claim #{number} step FAILED: {out[:200]}", file=sys.stderr)
            return []
    return [to_board_item(issue)]


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: board_github.py file <title> [--context <body>] [--lane <lane>] "
              "[--priority <high|medium|low>] | "
              "claim <worker> <n> | claim-item <worker> <number> | list | done <number> [note] | release <number> [note]",
              file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "file":
        if len(args) < 2:
            print("file needs a title", file=sys.stderr)
            return 2
        title = args[1]
        body, lane, priority = "", "", ""
        rest = args[2:]
        i = 0
        while i < len(rest):
            if rest[i] == "--context" and i + 1 < len(rest):
                body = rest[i + 1]; i += 2
            elif rest[i] == "--lane" and i + 1 < len(rest):
                lane = rest[i + 1]; i += 2
            elif rest[i] == "--priority" and i + 1 < len(rest):
                priority = rest[i + 1]; i += 2
            else:
                i += 1
        return 0 if file_item(title, body, lane, priority) else 1
    if cmd == "claim":
        if len(args) != 3:
            print("claim needs <worker> <n>", file=sys.stderr)
            return 2
        print(json.dumps(claim_next_n(args[1], int(args[2]))))
        return 0
    if cmd == "claim-item":
        if len(args) != 3:
            print("claim-item needs <worker> <number>", file=sys.stderr)
            return 2
        print(json.dumps(claim_item(args[1], int(args[2]))))
        return 0
    if cmd == "list":
        print(json.dumps(list_unclaimed()))
        return 0
    if cmd == "done":
        if len(args) < 2:
            print("done needs <number>", file=sys.stderr)
            return 2
        rc, out = _run(build_done_cmd(int(args[1]), " ".join(args[2:])))
        if rc != 0:
            print(f"board_github: done FAILED: {out[:200]}", file=sys.stderr)
        return rc
    if cmd == "release":
        if len(args) < 2:
            print("release needs <number> [note]", file=sys.stderr)
            return 2
        return 0 if release_item(int(args[1]), " ".join(args[2:])) else 1
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
