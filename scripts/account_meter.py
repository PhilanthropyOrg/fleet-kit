#!/usr/bin/env python3
"""account_meter.py -- per-account week meter rows, read so one blind meter never stops a build.

2026-10-16, Reif: "is g actually running fleet to its max potential?" It was not. maxx answered
HTTP 502 every pass, gru_allowance.py printed nothing, and gru built nothing "because the
allowance meter was unreadable" -- while the phil account had 87% of its week left at 84% of
the week elapsed, resetting in a day. And FLEET_MINION_MAX_PER_HOUR=1 held the builder to one
start an hour whatever the week had left (minion: 0.71 runs/h against judge-judy's 9.8).

FLEET_METER_FILE points at a JSON file of per-account rows (G's meter.py writes this shape):
    {"ts": ..., "rows": [{"name": "phil", "week_left": 87, "elapsed": 84, "walled": false},
                         {"name": "tgp", "error": "no reading: long-lived setup token ..."}]}

  - A row with `week_left` and `elapsed` READS. It has room when it is not walled and
    week_left is above RESERVE_PCT.
  - A row with an `error` is UNREADABLE, not empty: the account is still usable unless it
    walls (a 429/limit on a real call, which the row records as `walled: true`).
  - pace_multiplier(): how many times the even pace an account must spend to land on the
    reserve at reset: (week_left - RESERVE_PCT) / time_left. phil above: (87-10)/16 = 4.8.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RESERVE_PCT = 10.0           # never plan to spend the last 10% of any account's week
GAP_PTS = 10.0               # budget left this far over week left: the builder cap steps up (tank.py)
DEFAULT_FILE = "/fleet-kit/shares/g/meter.json"  # G's host cron copies ~/G/meter.json here every 15 min
MAX_PACED_PER_HOUR = 6       # gru's concurrency ceiling is 7; leave one slot for the rest
STALE_S = 3 * 3600           # an older file is no reading at all


def load(path: str | None = None, now: float | None = None) -> list[dict]:
    """Rows from FLEET_METER_FILE, or [] when unset, missing, malformed or stale."""
    path = path if path is not None else (os.environ.get("FLEET_METER_FILE") or DEFAULT_FILE)
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or now - float(data.get("ts") or 0) > STALE_S:
        return []
    return [r for r in data.get("rows") or [] if isinstance(r, dict)]


def reads(row: dict) -> bool:
    return not row.get("error") and row.get("week_left") is not None and row.get("elapsed") is not None


def has_room(row: dict) -> bool:
    """A readable account with week left above the reserve, or an unreadable one that has not walled."""
    if row.get("walled"):
        return False
    return float(row["week_left"]) > RESERVE_PCT if reads(row) else True


def pace_multiplier(row: dict) -> float:
    """Times the even pace this account must spend to reach RESERVE_PCT at reset; 0 if it cannot."""
    if not reads(row) or row.get("walled"):
        return 0.0
    time_left = max(100.0 - float(row["elapsed"]), 1.0)
    return max(float(row["week_left"]) - RESERVE_PCT, 0.0) / time_left


def pace_gap(row: dict) -> float | None:
    """Points by which budget left beats week left (phil 10-16: 87 - 16 = +71); None if unread."""
    if not reads(row):
        return None
    return float(row["week_left"]) - (100.0 - float(row["elapsed"]))


def best(rows: list[dict]) -> dict | None:
    """The readable account furthest behind pace, or None when no readable account has room."""
    readable = [r for r in rows if reads(r) and has_room(r)]
    return max(readable, key=pace_multiplier, default=None)


def paced_hour_cap(base: int, rows: list[dict]) -> int:
    """The hour cap scaled by the best account's pace gap: never below `base`, never above
    MAX_PACED_PER_HOUR (unless base itself is higher). base 0 (no cap) stays 0."""
    row = best(rows)
    if not base or row is None:
        return base
    step = 1 if (pace_gap(row) or 0) >= GAP_PTS else 0  # Reif 10-17: a 10-point gap is one more start
    return max(base + step, min(round(base * pace_multiplier(row)), MAX_PACED_PER_HOUR))
