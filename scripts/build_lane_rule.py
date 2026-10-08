#!/usr/bin/env python3
"""build_lane_rule.py -- the instance's build lane rule and hour cap, in one place, fail-closed.

2026-10-08, Reif: "we listen hard for issues, and only build fixes for issues, and then the
one funnel thing we need - orgverify ... cut the prs way down, one per hour - big moves".
The last 100 merged PRs moved no outcome number (claims flat, verified flat, sign-ups down a
third) and cost ~5,850 CI minutes a day against a 50,000-minute month; 145 minion starts in
the 24h before this landed.

Two dials, both read from fleet.env. Unset = the old behaviour, nothing changes.
  FLEET_BUILD_ONLY_LABELS    comma list. A minion item must carry at least one of them.
  FLEET_MINION_MAX_PER_HOUR  at most this many minion starts in any rolling hour.

Three places enforce them, so a prompt cannot build around the rule:
  gate_drops.py candidates   drops off-lane items before gru claims them (step 2b);
  fanout.py batches          returns only the batches the hour still has room for (step 5);
  run_member.sh              refuses an off-lane or over-cap minion at dispatch, in one second
                             (gru's 2a reif-priority and 8b standing-lane paths skip the packer).

Run: python3 scripts/build_lane_rule.py check --items 1,2 --repo owner/repo
     python3 scripts/build_lane_rule.py hour-count --runs /var/log/fleet-kit/runs.jsonl
     python3 scripts/build_lane_rule.py reserve --runs /var/log/fleet-kit/runs.jsonl \
         --ledger /var/log/fleet-kit/minion_hour_starts.log --cap 1   # exit 3 = hour full
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Mapping


def allowed_labels(env: Mapping[str, str] | None = None) -> list[str]:
    """FLEET_BUILD_ONLY_LABELS as a clean list; [] when unset or blank (= no rule)."""
    raw = (env if env is not None else os.environ).get("FLEET_BUILD_ONLY_LABELS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def off_lane(item_labels: Mapping[int, Iterable[str]], allowed: Iterable[str]) -> list[int]:
    """Pure. Item numbers that carry none of `allowed`. No rule (empty allowed) -> []."""
    want = set(allowed)
    if not want:
        return []
    return [n for n, labels in item_labels.items() if not want & set(labels)]


def hour_cap(env: Mapping[str, str] | None = None) -> int:
    """FLEET_MINION_MAX_PER_HOUR as an int; 0 when unset, blank or not a number (= no cap)."""
    raw = (env if env is not None else os.environ).get("FLEET_MINION_MAX_PER_HOUR", "")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def started_in_window(lines: Iterable[str], member: str = "minion", window_s: float = 3600,
                      now: float | None = None) -> int:
    """Pure. `started` rows for `member` in runs.jsonl whose ts is inside the last window_s.
    A malformed line, or a row with no usable ts, is skipped (it cannot be inside the window)."""
    now = time.time() if now is None else now
    n = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("member") != member or rec.get("status") != "started":
            continue
        try:
            ts = float(rec.get("ts") or rec.get("recorded_at") or 0)
        except (TypeError, ValueError):
            continue
        if now - window_s < ts <= now:
            n += 1
    return n


def reserve(runs: Path, ledger: Path, cap: int, member: str = "minion", window_s: float = 3600,
            now: float | None = None) -> tuple[bool, int]:
    """Count and take an hour slot as one step, under a lock on `ledger`. Returns (taken, count
    before). 2026-10-08 04:12Z: four minions launched within 11s and each wrote its `started`
    row 9-30s after its own cap check, so all four counted 0 against a cap of 1. The ledger line
    is written before the lock is released, so the next dispatcher sees this start at once.
    count = the larger of runs.jsonl starts and ledger lines, so starts from any path count."""
    now = time.time() if now is None else now
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        held = 0
        for line in fh.read().splitlines():
            try:
                ts = float(line.split()[0])
            except (ValueError, IndexError):
                continue
            # No upper bound: a line is only written under this lock, so one stamped a few ms
            # after our `now` is a racer that won the lock first (CI 2026-10-08: two of four
            # dispatchers got the one slot when the loser's clock read before the winner's write).
            if ts > now - window_s:
                held += 1
        lines = runs.read_text().splitlines() if runs.exists() else []
        count = max(held, started_in_window(lines, member=member, window_s=window_s, now=now))
        if count >= cap:
            return False, count
        fh.write(f"{now} {member} {os.getpid()}\n")
        fh.flush()
        return True, count


def remaining(cap: int, started: int) -> int | None:
    """Pure. Minion starts the hour still allows; None when there is no cap."""
    if cap <= 0:
        return None
    return max(0, cap - started)


def cap_batches(result: dict, room: int | None, cap: int = 0, started: int = 0) -> dict:
    """Pure. Keep the first `room` batches of a fanout.py `batches` result; the rest of the
    items go to `deferred` with a why that names the dial. None = no cap, result untouched."""
    if room is None:
        return result
    batches = result.get("batches") or []
    if len(batches) <= room:
        return result
    out = dict(result)
    kept, dropped = batches[:room], batches[room:]
    why = (f"hour cap: FLEET_MINION_MAX_PER_HOUR={cap}, {started} minion(s) already started "
           f"in the last hour; next pass")
    out["batches"] = kept
    out["n_batches"] = len(kept)
    out["deferred"] = list(result.get("deferred") or []) + [
        {**it, "why": why} for b in dropped for it in b.get("items", [])]
    out["hour_cap"] = {"cap": cap, "started_last_hour": started, "room": room,
                       "batches_deferred": len(dropped)}
    return out


def _labels_via_gh(numbers: list[int], repo: str) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for n in numbers:
        r = subprocess.run(["gh", "issue", "view", str(n), "--repo", repo, "--json", "labels",
                            "--jq", "[.labels[].name]|join(\",\")"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"gh issue view {n}: {r.stderr.strip()[:200]}")
        out[n] = [s for s in r.stdout.strip().split(",") if s]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="print the item numbers that carry none of FLEET_BUILD_ONLY_LABELS")
    c.add_argument("--items", required=True, help="comma-separated issue numbers")
    c.add_argument("--repo", required=True)
    h = sub.add_parser("hour-count", help="print minion starts in the last hour from runs.jsonl")
    h.add_argument("--runs", required=True)
    h.add_argument("--member", default="minion")
    h.add_argument("--window-s", type=float, default=3600)
    r = sub.add_parser("reserve", help="take one hour slot atomically; exit 3 and print the count when full")
    r.add_argument("--runs", required=True)
    r.add_argument("--ledger", required=True)
    r.add_argument("--cap", type=int, required=True)
    r.add_argument("--member", default="minion")
    r.add_argument("--window-s", type=float, default=3600)
    a = ap.parse_args(argv)
    if a.cmd == "check":
        allowed = allowed_labels()
        if not allowed:
            return 0
        numbers = [int(s) for s in a.items.split(",") if s.strip()]
        try:
            labels = _labels_via_gh(numbers, a.repo)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            # Fail closed: an item whose labels cannot be read is not known to be on lane.
            print(f"could-not-read-labels: {exc}", file=sys.stderr)
            print(",".join(str(n) for n in numbers))
            return 0
        bad = off_lane(labels, allowed)
        print(",".join(str(n) for n in bad))
        return 0
    if a.cmd == "hour-count":
        p = Path(a.runs)
        lines = p.read_text().splitlines() if p.exists() else []
        print(started_in_window(lines, member=a.member, window_s=a.window_s))
        return 0
    if a.cmd == "reserve":
        taken, count = reserve(Path(a.runs), Path(a.ledger), a.cap, a.member, a.window_s)
        print(count)
        return 0 if taken else 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
