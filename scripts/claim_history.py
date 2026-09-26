#!/usr/bin/env python3
"""claim_history -- has this item already been claimed-and-abandoned too many times?

THE GAP THIS CLOSES (gh#64). gru.md step 2b filters candidates by `fleet:claimed` and
`fleet:needs-human-op`, but nothing distinguishes "never tried" from "tried and dead-ended N
times" -- the same chronically-blocked item gets reclaimed and respawned every hour, burning a
full claim/spawn/clear cycle each time, because nothing upstream of the claim step counts prior
attempts.

WHAT COUNTS AS A DEAD END (Reif, 2026-09-26). Only a minion saying so. A run counts
against item N only when its report carried an explicit `Blocked: #N <reason>` line
(run_report.py stores those lines in the run's `blocked` field). Nothing else counts:
  - a kill, timeout or other infra status (killed/rc=143, timed_out/rc=124, budget_declined,
    paced, dispatch_skipped, report_lost, ...). The minion never got to decide anything.
  - a run that pushed a checkpoint draft PR (`checkpoint_pr`, run_member.sh). That is
    progress the next pass resumes, not a blocker.
  - an `ok`/`quiet` run that shipped a Part-of PR and left the item open. Partial progress is
    not a dead end.
Before this, every minion run against a still-open item counted, whatever its status. On
2026-09-26 five fleet:reif-priority items (philanthropy#7939/7940/7942/7948/7950) went
fleet:dead-end-blocked at count 3-4. Their "dead ends" were deploy-drain SIGTERMs, 5400s
timeouts from before the checkpoint fix (fk#1310), and Part-of PRs that had shipped real work.

Pure core (`dead_end_claim_count`/`is_dead_end_blocked`), thin DB seam
(`minion_runs_for_item`), CLI (`main`) -- same split as cost_bridge.py.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402

# UNKNOWN in gh#64's own PRD: "the exact dead-end threshold (N claims within what time window)
# ... needs a human call or a reasoned default stated explicitly in the PR." This is that
# reasoned default, not a human call:
#   - 3 strikes: 1-2 prior claims is well within "just needed another try" (real items in this
#     fleet's own history have succeeded on a 2nd or 3rd claim); 3 dead ends with the issue
#     still open is the point gru.md's own filing of this issue calls "chronically blocked."
#   - 14 days: long enough to span many of gru's real hourly passes (so a genuinely dead item
#     gets caught, not just one still cooling from its last attempt), short enough that an item
#     fixed, reopened, and now being retried fresh does not inherit a stale strike count from
#     months back.
DEFAULT_DEAD_END_THRESHOLD = 3
DEFAULT_WINDOW_DAYS = 14.0


# Terminal statuses that mean the runner, not the minion, ended the pass. Never a dead end,
# even if a Blocked: line somehow made it into the record. `started` is gh#145's provisional
# row and never a terminal verdict.
INFRA_STATUSES = frozenset({
    "started", "killed", "timed_out", "budget_declined", "paced", "dispatch_skipped",
    "heartbeat", "report_lost", "incomplete_fanout",
})

_ITEM_REF = re.compile(r"#(\d+)\b")


def blocked_applies_to(blocked: str | None, item_number: int) -> bool:
    """Does a run's `blocked` field (newline-joined `Blocked:` line bodies) block `item_number`?

    A line that names issue numbers (`#7939 needs Resend log access`) blocks exactly those.
    A line that names none (`Blocked: prod env var FOO is unset`) blocks the whole run's
    batch -- the minion said it could not go on, and didn't narrow it. Empty reason = no
    block: `Blocked:` alone is not an explanation."""
    for line in (blocked or "").splitlines():
        line = line.strip()
        if not line:
            continue
        refs = {int(n) for n in _ITEM_REF.findall(line)}
        if not refs:
            return True
        if item_number in refs and _ITEM_REF.sub("", line).strip(" -:;,.\u2013\u2014"):
            return True
    return False


def is_dead_end_run(status: str | None, exit_code: int | None, blocked: str | None,
                    checkpoint_pr, item_number: int) -> bool:
    """One terminal run row: did it end with the minion explicitly blocked on this item?"""
    if (status or "") in INFRA_STATUSES:
        return False
    if exit_code not in (None, 0):
        return False
    if checkpoint_pr not in (None, "", 0, "0"):
        return False
    return blocked_applies_to(blocked, item_number)


def dead_end_claim_count(run_ids: list[str], item_number: int) -> int:
    """How many DISTINCT `run_ids` are minion runs against `item_number`.

    `run_ids` should already be scoped by the caller to the recent window and to
    member='minion' (see `minion_runs_for_item`) -- this only matches the run_id SHAPE gru.md
    step 7 already documents minion's own runs follow: `minion-item<n>-<pid>-<timestamp>`.

    gh#4966: `runs` writes two rows per real attempt (a `started` row and a terminal-status
    row) sharing one `run_id` -- de-duping here, on top of `minion_runs_for_item`'s own
    `SELECT DISTINCT`, means this still counts correctly even if a future caller passes in an
    undeduped list.
    """
    prefix = f"minion-item{item_number}-"
    return sum(1 for run_id in set(run_ids) if (run_id or "").startswith(prefix))


def is_dead_end_blocked(run_ids: list[str], item_number: int,
                        threshold: int = DEFAULT_DEAD_END_THRESHOLD) -> bool:
    return dead_end_claim_count(run_ids, item_number) >= threshold


def minion_runs_for_item(conn, item_number: int,
                         window_days: float = DEFAULT_WINDOW_DAYS) -> list[str]:
    """DISTINCT run_ids from fleet.db: minion runs against `item_number` in the last
    `window_days` that ended as a dead end per `is_dead_end_run` (an explicit `Blocked:` for
    this item, no infra status, no checkpoint draft). See the module docstring.

    gh#4966: `runs` has a composite (run_id, recorded_at) primary key, so a single real run
    (started + terminal-status rows) is two rows sharing one `run_id`. Grepped every caller of
    this function (just `main()` below, plus selftest.py) -- none wants the raw duplicate rows
    (no cost/duration calc reads this), so de-duping at the query is the smaller, correct fix.

    2026-09-14 (minion batching, gru.md step 5): a batched minion's `item_id` is an
    underscore-joined list ("64_99_143"), not one bare number -- run_member.sh's RUN_ID uses
    the same join so both stay consistent with each other, but it means an exact `item_id = ?`
    match here would silently stop finding a batched run's claim history for every item except
    the one lucky enough to be alone in its batch. Matched as a whole token instead: GLOB, not
    LIKE -- SQLite's LIKE treats `_` as a single-character wildcard, which would make the `_`
    boundary markers below match ANY character and let item 6 falsely match a batch containing
    64 or 164; GLOB's `*` has no such special-casing of `_`, so the underscore boundaries here
    are literal. Verified: item 6 against item_ids ("64","64_99","6_164","164") matches only
    "6_164"; item 64 matches only "64" and "64_99".
    """
    out: list[str] = []
    for run_id, status, exit_code, blocked, checkpoint_pr in _terminal_rows(
            conn, item_number, window_days):
        if run_id not in out and is_dead_end_run(status, exit_code, blocked, checkpoint_pr,
                                                 item_number):
            out.append(run_id)
    return out


def minion_attempts_for_item(conn, item_number: int,
                             window_days: float = DEFAULT_WINDOW_DAYS) -> int:
    """Every DISTINCT minion run against the item in the window, dead end or not -- printed
    beside the count so a reader sees "7 attempts, 0 blocked" instead of guessing."""
    return len({r[0] for r in _terminal_rows(conn, item_number, window_days)})


def _terminal_rows(conn, item_number: int, window_days: float):
    since = time.time() - window_days * 86400
    return conn.execute(
        "SELECT run_id, status, exit_code, blocked, checkpoint_pr FROM runs"
        " WHERE member = 'minion' AND recorded_at >= ? AND COALESCE(status, '') != 'started'"
        " AND ('_' || item_id || '_') GLOB ('*_' || ? || '_*')"
        " ORDER BY recorded_at",
        (since, str(item_number)),
    ).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Has this item already been claimed-and-abandoned past the dead-end "
                     "threshold? Exit 1 (BLOCKED) if so -- gru.md's claim step should skip it "
                     "and name it in the report, not silently reclaim it.")
    ap.add_argument("--item", type=int, required=True)
    ap.add_argument("--labels", default="", help="comma list of the item's labels; quality:world-class is never a dead end")
    ap.add_argument("--threshold", type=int, default=DEFAULT_DEAD_END_THRESHOLD)
    ap.add_argument("--window-days", type=float, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--db-path", help="override fleet.db path (default: fleet_db.DB_FILE)")
    a = ap.parse_args(argv)

    conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
    fleet_db.sync(conn)
    run_ids = minion_runs_for_item(conn, a.item, window_days=a.window_days)
    if "quality:world-class" in {l.strip() for l in (a.labels or "").split(",")}:
        # Reif, 2026-09-08: "get the spec up to par." A world-class item cycles research ->
        # VP review -> redo by design (members/vp/vp.md caps it at three Not-yet rounds); each
        # cycle is a minion run against a still-open issue, which is exactly what this filter
        # reads as a dead end. philanthropy#4863 hit BLOCKED count=4 mid-loop. vp owns the cap.
        print(f"ok world-class (iteration is the process; vp.md caps rounds) item={a.item}")
        return 0
    count = dead_end_claim_count(run_ids, a.item)
    blocked = is_dead_end_blocked(run_ids, a.item, threshold=a.threshold)
    attempts = minion_attempts_for_item(conn, a.item, window_days=a.window_days)
    print(f"{'BLOCKED' if blocked else 'ok'} count={count} threshold={a.threshold} "
          f"attempts={attempts}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
