#!/usr/bin/env python3
"""dumbledore's llm.pregate: skip the pass when it could only end in `Prediction: none`.

10 of dumbledore's 18 passes on 10-12..10-13 (~$10) wrote `Prediction: none -- rows open`:
the charter allows one open row per member and keeps one outcome bet open, so with three
rows open, a ladder_* row among them, no verdict landed since the last check and no ask
waiting, a pass has nothing to bet and nothing to learn. Prints `green <why>` then (quiet,
no model) or `FIRE <why>`. Fails open: any read error FIREs. Unacked INBOX mail is checked
by run_member.sh after a green pregate, so it is not checked here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_OPEN = 3  # at or above this many open rows a pass declines to add one


def decide(rows: list[dict], open_asks: int, since: float) -> str:
    mine = [r for r in rows if r.get("member") == "dumbledore"]
    open_rows = [r for r in mine if r.get("status") == "open"]
    fresh = [r for r in mine if r.get("status") != "open" and float(r.get("resolved_ts") or 0) > since]
    if open_asks:
        return f"FIRE {open_asks} open ask(s) to triage"
    if fresh:
        return "FIRE new verdict(s): " + ", ".join(f"#{r.get('id')} {r.get('status')}" for r in fresh[-3:])
    if len(open_rows) < MAX_OPEN:
        return f"FIRE {len(open_rows)} open row(s), room for a bet"
    if not any(str(r.get("metric", "")).startswith("ladder_") for r in open_rows):
        return "FIRE no open ladder_* outcome bet"
    nxt = min(float(r.get("due_ts") or 0) for r in open_rows)
    return (f"green {len(open_rows)} rows open incl. a ladder bet, no new verdict, no open ask; "
            f"next due ts={int(nxt)}")


def main() -> int:
    log_dir = Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))
    try:
        rows = [json.loads(l) for l in (log_dir / "predictions.jsonl").read_text().splitlines() if l.strip()]
        out = subprocess.run([sys.executable, str(HERE / "ask.py"), "list", "--status", "open"],
                             capture_output=True, text=True, timeout=60, check=True).stdout
        asks = json.loads(out or "[]")
        mark = log_dir / "dumbledore.pregate"  # run_member.sh rewrites it after this returns
        since = mark.stat().st_mtime if mark.exists() else 0.0
    except Exception as e:  # noqa: BLE001 -- fail open
        print(f"FIRE pregate could not read state ({type(e).__name__})")
        return 0
    print(decide(rows, len(asks), since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
