#!/usr/bin/env python3
"""open_runs -- which member runs are still in flight, from runs.jsonl, across containers.

THE GAP (2026-09-26 16:39 UTC). A deploy leaves the old build running as `philanthropy-retired`
until its passes finish. Its minions (#7939/#7941/#7950, resuming their checkpoints) are
invisible to the new container: not in its /proc, and their worktrees are not in its /tmp.
stale_claims.py read "no live runner" and could release their claims. run_member.sh's resume
read "holder path gone" and could remove a live worktree's entry. runs.jsonl lives on the shared
log dir, so a `started` row with no terminal row yet is a live run, wherever it runs.

  open_runs.py items                  # JSON list of item numbers with an open minion run
  open_runs.py has --prefix <run_id prefix>   # exit 0 if a run with that prefix is open

Open = a `started` row, no later row for the same run_id, started within FLEET_OPEN_RUN_MAX_S
(default 7200s: minion timeout_s 5400 plus slack). An older open row is a run SIGKILLed before
it could write its terminal row, so it does not hold anything forever.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

MAX_AGE_S = float(os.environ.get("FLEET_OPEN_RUN_MAX_S") or 7200)
TAIL_BYTES = 8 * 1024 * 1024
ITEMS_RE = re.compile(r"^[a-z-]+?-item(\d+(?:_\d+)*)-")


def runs_file() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "runs.jsonl"


def _tail_records(path: Path, tail_bytes: int = TAIL_BYTES):
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > tail_bytes:
        lines = lines[1:]  # first line is likely cut
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def open_run_ids(records, now: float, max_age_s: float = MAX_AGE_S) -> list[str]:
    """Pure: run_ids whose newest row is `started` and younger than max_age_s."""
    last: dict[str, dict] = {}
    for r in records:
        rid = r.get("run_id")
        if rid:
            last[rid] = r
    out = []
    for rid, r in last.items():
        if r.get("status") != "started":
            continue
        t = r.get("ts") or r.get("_recorded_at") or r.get("started_at") or 0
        if now - float(t or 0) <= max_age_s:
            out.append(rid)
    return out


def open_items(records, now: float, member: str = "minion", max_age_s: float = MAX_AGE_S) -> set[int]:
    items: set[int] = set()
    for rid in open_run_ids(records, now, max_age_s):
        if not rid.startswith(f"{member}-item"):
            continue
        m = ITEMS_RE.match(rid)
        if m:
            items.update(int(n) for n in m.group(1).split("_"))
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("items")
    h = sub.add_parser("has")
    h.add_argument("--prefix", required=True)
    a = ap.parse_args(argv)
    recs = _tail_records(runs_file())
    now = time.time()
    if a.cmd == "items":
        print(json.dumps(sorted(open_items(recs, now))))
        return 0
    return 0 if any(r.startswith(a.prefix) for r in open_run_ids(recs, now)) else 1


if __name__ == "__main__":
    sys.exit(main())
