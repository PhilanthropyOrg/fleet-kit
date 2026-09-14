#!/usr/bin/env python3
"""dead_end_label -- makes gru's dead-end guard (claim_history.py) visible on the board.

THE GAP THIS CLOSES (philanthropy#5934). `claim_history.is_dead_end_blocked` tells gru.md step
3 to drop a chronically-reclaimed item from its candidate set, but nothing writes that decision
anywhere a person or a later pass can see -- gru just silently omits the item. Marie found 30
`fleet:priority-high` items in exactly this state on 2026-09-14, including philanthropy#2195,
the fleet's own oldest open issue -- and this tracking issue itself had been silently dropped
the same way.

Split the same shape as board_github.py: pure PLANNERS decide what to do from an issue's
current labels/comments (unit-tested, no network, no `gh` call); `main()` is the thin exec
seam. `claim_history.py` stays a pure, side-effect-free predicate -- the PRD's own weak
preference -- this module is "the caller" it left that choice to.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
LABEL_DEAD_END_BLOCKED = f"{PREFIX}dead-end-blocked"

# Matches this module's own comment body (below) so a re-run can tell "already said this" from
# "count changed since we last said it."
_COMMENT_RE = re.compile(r"^dead-end-blocked: count=(\d+) threshold=(\d+)", re.MULTILINE)


def _label_names(labels) -> list[str]:
    return [lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in labels or []]


def is_labeled(issue: dict) -> bool:
    return LABEL_DEAD_END_BLOCKED in _label_names(issue.get("labels"))


def _existing_counts(comments) -> set[int]:
    counts: set[int] = set()
    for c in comments or []:
        m = _COMMENT_RE.search(c.get("body") or "")
        if m:
            counts.add(int(m.group(1)))
    return counts


def block_comment_body(count: int, threshold: int, run_id: str) -> str:
    return (
        f"dead-end-blocked: count={count} threshold={threshold} run={run_id} -- gru dropped "
        f"this item from its candidate set this pass (see `scripts/claim_history.py`). Fix the "
        f"underlying blocker and remove the `{LABEL_DEAD_END_BLOCKED}` label to let the next "
        f"pass retry it -- gru re-evaluates the current count either way, so a premature clear "
        f"just re-blocks with a fresh comment instead of staying cleared."
    )


def plan_block(issue: dict, count: int, threshold: int, run_id: str) -> list[list[str]]:
    """Commands to run when claim_history.py reports BLOCKED for `issue`.

    Idempotent while the label is already on: a comment for this exact `count` is not repeated
    (the once-per-hour spam philanthropy#5934 exists to stop). But the label being ABSENT --
    never applied, or a human cleared it by hand -- always gets a fresh comment even if the
    count is unchanged, so a manual clear is a real retry with its own paper trail, not a
    permanent silence (criterion 3).
    """
    number = issue["number"]
    cmds: list[list[str]] = []
    labeled = is_labeled(issue)
    if not labeled:
        cmds.append(["gh", "issue", "edit", str(number), "--add-label", LABEL_DEAD_END_BLOCKED])
    if not labeled or count not in _existing_counts(issue.get("comments")):
        cmds.append(["gh", "issue", "comment", str(number),
                     "--body", block_comment_body(count, threshold, run_id)])
    return cmds


def plan_unblock(issue: dict) -> list[list[str]]:
    """Commands to run once an item's count has aged back under threshold: strip the label so
    it re-enters gru's candidate set (criterion 2). No-op if it was never labeled."""
    if not is_labeled(issue):
        return []
    return [["gh", "issue", "edit", str(issue["number"]), "--remove-label", LABEL_DEAD_END_BLOCKED]]


def _run(cmd: list[str]) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {out.stderr.strip()[:200]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Apply or clear fleet:dead-end-blocked on one issue per "
                     "claim_history.py's verdict. Reads the issue's current labels+comments "
                     "itself (one `gh issue view` call) so gru.md's loop only has to pass the "
                     "number and claim_history.py's own output.")
    ap.add_argument("--item", type=int, required=True)
    ap.add_argument("--blocked", action="store_true",
                     help="claim_history.py exited 1 (BLOCKED) for this item this pass")
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--threshold", type=int, default=3)
    ap.add_argument("--run-id", default="")
    a = ap.parse_args(argv)

    out = subprocess.run(
        ["gh", "issue", "view", str(a.item), "--json", "number,labels,comments"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        print(f"error: gh issue view {a.item} failed: {out.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    issue = json.loads(out.stdout)

    cmds = plan_block(issue, a.count, a.threshold, a.run_id) if a.blocked else plan_unblock(issue)
    for cmd in cmds:
        _run(cmd)
    print(f"{'blocked' if a.blocked else 'checked'} item={a.item} actions={len(cmds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
