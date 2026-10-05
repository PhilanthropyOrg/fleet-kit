#!/usr/bin/env python3
"""Should gru's nerd dispatch for <lane> be skipped as a repeat-quiet look?

Measured 2026-10-05: gru re-sends a nerd to claim/datadog/growth every pass, and 28 of 55 nerd
passes a day spent ~3 turns ($4.24/day) only to write "QUIET-REPEAT: the last full pass was
quiet". That is a $0 decision, so dispatch_member.sh makes it here, before a model exists.

Prints one line (the reason) and exits 0 when the lane's newest FULL pass (not a QUIET-REPEAT)
was `quiet` and is younger than the window (FLEET_NERD_REPEAT_QUIET_S, default 4h: the
staleness bound nerd.md already used). Prints nothing otherwise. Any read error prints nothing,
so the nerd runs (fail open).

  python3 nerd_repeat_quiet.py <lane> [<fleet.db>]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time


def reason(lane: str, db: str, window_s: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            "SELECT recorded_at, status FROM runs WHERE member='nerd' AND lane=?"
            " AND status IN ('ok','quiet') AND coalesce(outcome,'') NOT LIKE 'QUIET-REPEAT%'"
            " ORDER BY recorded_at DESC LIMIT 1", (lane,)).fetchone()
    except sqlite3.Error:
        return ""
    if not row or row[1] != "quiet" or now - float(row[0]) >= window_s:
        return ""
    at = time.strftime("%H:%MZ", time.gmtime(float(row[0])))
    return f"last full pass {at} was QUIET, {int((now - float(row[0])) // 60)}m ago"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    window = float(os.environ.get("FLEET_NERD_REPEAT_QUIET_S", "14400"))
    if window <= 0:
        return 0
    db = argv[1] if len(argv) > 1 else os.path.join(
        os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"), "fleet.db")
    r = reason(argv[0], db, window)
    if r:
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
