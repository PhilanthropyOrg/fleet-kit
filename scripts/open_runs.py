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
  open_runs.py close-lost             # append a terminal row for every run that never got one

close-lost (gh#8197): run_member.sh records every ending it can see -- normal exit, timeout
(124), SIGTERM/SIGINT (143, record_killed_pass). What no trap can see is SIGKILL: an OOM kill,
`podman stop` escalating, a host reboot. Those runs stayed `started` forever (seven such rows
from 09-23/09-24 were still open on 2026-09-26). A `started` row with no later row, older than
its own timeout_s + FLEET_LOST_SLACK_S (3600: the concurrency-slot queue and the timeout
checkpoint both sit outside timeout_s), cannot still be running: close-lost writes it a
status=killed row, exit_code 137, lost=true, with an outcome that says so. Runs from gru's
cron entry before every pass; a flock keeps two containers from closing the same run twice.

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


LOST_SLACK_S = float(os.environ.get("FLEET_LOST_SLACK_S") or 3600)
LOST_EXIT_CODE = 137  # 128+9: the only ending run_member.sh cannot record itself


def _row_ts(r: dict) -> float:
    return float(r.get("ts") or r.get("_recorded_at") or r.get("started_at") or 0)


def lost_runs(records, now: float, slack_s: float = LOST_SLACK_S,
              default_timeout_s: float = MAX_AGE_S) -> list[dict]:
    """Pure: the `started` rows whose run can no longer be alive and never wrote an ending."""
    last: dict[str, dict] = {}
    for r in records:
        rid = r.get("run_id")
        if rid:
            last[rid] = r
    out = []
    for rid, r in last.items():
        if r.get("status") != "started":
            continue
        try:
            timeout_s = float(r.get("timeout_s") or default_timeout_s)
        except (TypeError, ValueError):
            timeout_s = default_timeout_s
        if now - _row_ts(r) > timeout_s + slack_s:
            out.append(r)
    return out


def lost_record(started: dict, now: float) -> dict:
    """The terminal row for a lost run: the shape run_report.build_record writes, exit 137."""
    import run_report
    rec = run_report.build_record(
        member=started.get("member") or "unknown", run_id=started["run_id"],
        kind=started.get("kind") or "llm", exit_code=LOST_EXIT_CODE, pass_text="", usage=None,
        vision_required=False, item_id=started.get("item_id"), lane=started.get("lane"),
        fired_by=started.get("fired_by"), reason=started.get("reason"))
    age_min = int((now - _row_ts(started)) // 60)
    rec["outcome"] = (f"LOST -- started {age_min} min ago and never recorded an ending; its process "
                      f"is gone (SIGKILL, OOM, container stop or reboot). Recorded as killed, "
                      f"exit {LOST_EXIT_CODE}, by open_runs.py close-lost; safe to re-run.")
    rec["lost"] = True
    rec["ts"] = now
    return rec


def close_lost(path: Path, now: float) -> list[dict]:
    """Append a lost_record for every lost run in `path`, under an exclusive flock so two
    containers sweeping the shared file at once never close the same run twice."""
    import fcntl
    if not path.exists():
        return []
    with open(str(path) + ".close-lost.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recs = [lost_record(r, now) for r in lost_runs(_tail_records(path), now)]
        if recs:
            with path.open("a") as f:
                f.write("".join(json.dumps(r) + "\n" for r in recs))
        return recs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("items")
    h = sub.add_parser("has")
    h.add_argument("--prefix", required=True)
    sub.add_parser("close-lost")
    a = ap.parse_args(argv)
    if a.cmd == "close-lost":
        for r in close_lost(runs_file(), time.time()):
            print(f"closed lost run {r['run_id']} (member={r['member']}, item={r.get('item_id')}) "
                  f"-> status={r['status']} exit_code={r['exit_code']}")
        return 0
    recs = _tail_records(runs_file())
    now = time.time()
    if a.cmd == "items":
        print(json.dumps(sorted(open_items(recs, now))))
        return 0
    return 0 if any(r.startswith(a.prefix) for r in open_run_ids(recs, now)) else 1


if __name__ == "__main__":
    sys.exit(main())
