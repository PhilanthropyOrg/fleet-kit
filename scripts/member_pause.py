#!/usr/bin/env python3
"""member_pause -- fk#1429: POST /api/members/<name>/pause and /resume pause a member's
SCHEDULED and WOKEN passes. run_member.sh checks this before it would dispatch (skipping, same
as the existing per-member dispatch-lock skip); fleet_msg.wake() checks it before waking a
recipient. A pass already in flight when the flag is set is untouched -- this only gates the
NEXT dispatch.

The flag is one small JSON file per paused member, under FLEET_LOG_DIR -- the same host-mounted,
survives-a-redeploy directory fleet_msg.py's own wake-cooldown stamp already lives in (see its
_wake_stamp_path). Not fleet.db: run_member.sh runs as plain bash with no guaranteed sqlite3
binary, and a file flag is the same "must survive the thing it controls being broken" reasoning
fleet_enabled.sh's own header gives for FLEET_ENABLED.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _log_dir(log_dir=None) -> Path:
    return Path(log_dir or os.environ.get(
        "FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()


def _path(member: str, log_dir=None) -> Path:
    return _log_dir(log_dir) / "paused" / f"{member}.json"


def get(member: str, log_dir=None) -> dict | None:
    """{"member", "paused": True, "since", "by"} if `member` is paused, else None. Never raises
    on a missing or corrupt file -- an unreadable flag must read as "not paused", not crash the
    dispatcher that calls this before every pass."""
    try:
        return json.loads(_path(member, log_dir).read_text())
    except (OSError, ValueError):
        return None


def pause(member: str, by: str, log_dir=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    rec = {"member": member, "paused": True, "since": now, "by": by}
    p = _path(member, log_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec))
    return rec


def resume(member: str, log_dir=None) -> bool:
    """True if a pause flag existed and was cleared; False if it was already not paused."""
    p = _path(member, log_dir)
    if p.exists():
        p.unlink()
        return True
    return False
