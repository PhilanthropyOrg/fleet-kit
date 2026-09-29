#!/usr/bin/env python3
"""box_log_incidents.py -- the fleet reads the ONE log area and files what's actionable
(nonprofit-atlas#8703).

webhook_receiver.py's /webhook/box-log appends every line a prod box ships
(nonprofit-atlas scripts/box/log_ship.py) to logs/box-logs.jsonl. This is the HOST side: a
systemd .path unit fires it on every write; it reads what's new since its offset
(read_new_records, shared with notify_hq_prod_alert.py) and folds each record into one state
machine per incident key -- the key the box already put on every actionable line (``exit:<job>``,
``<job>:<slug>``):

  new key (ERROR / ALERT / non-zero EXIT)  -> ONE board item ``prod alert [<key>]``, through the
                                             same inbox.file_or_comment_alert the email pager uses
                                             (so a box-log key and a mailed page never twin)
  same key again                           -> counted; at most one comment per REPEAT_COMMENT_S
  the job's next EXIT without the key      -> closed ("recovered"), with the clean run's line --
    at least CLEAN_S after the key was last seen    only once the key has been gone CLEAN_S (box
                                             time), so a canary that fails every other run stays
                                             ONE open item instead of filing/closing all day
  nginx:/journal: keys (no run frames)     -> closed after QUIET_CLOSE_S with no repeat

State: logs/box-log-incidents.json ({key: {issue, job, first, last, count, commented}}).

Usage: box_log_incidents.py <box-logs.jsonl>
"""
from __future__ import annotations

import calendar
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from notify_reif_hq import read_new_records  # noqa: E402

REPEAT_COMMENT_S = 6 * 3600
QUIET_CLOSE_S = 24 * 3600
CLEAN_S = 30 * 60
UNFRAMED = ("nginx:", "journal:")


def actionable(r: dict) -> bool:
    return bool(r.get("key")) and (r.get("level") in ("ERROR", "ALERT") or bool(r.get("exit")))


def _t(r: dict, now: float) -> float:
    """The box's own timestamp for a record (a backlog arrives in one batch; `now` would
    squash an hour of runs into one instant). Falls back to `now`."""
    try:
        return float(calendar.timegm(time.strptime(str(r.get("ts"))[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return now


def fold(state: dict, records: list[dict], now: float) -> list[tuple]:
    """Pure: advance `state` over `records`; returns the actions to take, in order:
    ("open", key, rec) / ("repeat", key, rec, count) / ("close", key, why)."""
    actions: list[tuple] = []
    for r in records:
        if actionable(r):
            k = r["key"]
            inc = state.get(k)
            if inc is None:
                state[k] = {"issue": None, "job": r.get("job", ""), "first": now,
                            "last": _t(r, now), "count": 1, "commented": now}
                actions.append(("open", k, r))
            else:
                inc["count"] += 1
                inc["last"] = _t(r, now)
                if now - inc["commented"] >= REPEAT_COMMENT_S:
                    inc["commented"] = now
                    actions.append(("repeat", k, r, inc["count"]))
        if r.get("level") == "EXIT":
            ran, at = set(r.get("run_keys") or []), _t(r, now)
            for k in [k for k, v in state.items() if v["job"] == r.get("job") and k not in ran
                      and at - v["last"] >= CLEAN_S]:
                del state[k]
                actions.append(("close", k, f"recovered: {r.get('msg', '')}"))
    for k in [k for k, v in state.items()
              if v["job"].startswith(UNFRAMED) and now - v["last"] >= QUIET_CLOSE_S]:
        del state[k]
        actions.append(("close", k, "recovered: no repeat in 24h"))
    return actions


def _line(r: dict) -> str:
    return f"{r.get('host', '')} {r.get('job', '')} {r.get('ts', '')}: {r.get('msg', '')}"[:1000]


def apply(actions: list[tuple], state: dict, issues: dict, run=None) -> list[str]:
    """Side effects for fold()'s actions. `issues` = {key: issue number} as known before this
    batch; opens add to it, so a close -- even of an incident opened earlier in the same batch,
    which fold() already dropped from `state` -- can name its issue. One log line per action."""
    import inbox

    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    slug = inbox.repo_slug()
    out = []
    for a in actions:
        kind, key = a[0], a[1]
        try:
            if kind == "open":
                url, created = inbox.file_or_comment_alert(
                    {"check": key, "text": _line(a[2]), "from": "box-log (nonprofit-atlas#8703)"},
                    run=run)
                m = re.search(r"/issues/(\d+)", url or "")
                issues[key] = int(m.group(1)) if m else None
                if key in state:
                    state[key]["issue"] = issues[key]
                out.append(f"{'filed' if created else 'commented'} {key} -> {url}")
            elif kind == "repeat" and state.get(key, {}).get("issue"):
                run(["gh", "issue", "comment", "--repo", slug, str(state[key]["issue"]),
                     "--body", f"Still firing ({a[3]} lines so far): {_line(a[2])}"])
                out.append(f"repeat {key} x{a[3]}")
            elif kind == "close" and issues.get(key):
                run(["gh", "issue", "close", "--repo", slug, str(issues[key]),
                     "--comment", f"Auto-closed by box_log_incidents.py -- {a[2]}"[:1500]])
                out.append(f"closed {key} #{issues[key]}")
        except Exception as exc:  # noqa: BLE001 -- one bad action must not drop the rest
            if kind == "open":
                state.pop(key, None)  # not filed: the next firing tries again
            out.append(f"FAILED {kind} {key}: {exc}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: box_log_incidents.py <box-logs.jsonl>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    offset_path = path.with_suffix(path.suffix + ".offset")
    state_path = path.with_name("box-log-incidents.json")
    records, end = read_new_records(path, offset_path)
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    issues = {k: v.get("issue") for k, v in state.items()}
    actions = fold(state, records, time.time())
    for line in apply(actions, state, issues):
        print(f"box_log_incidents: {line}")
    state_path.write_text(json.dumps(state))
    offset_path.write_text(str(end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
