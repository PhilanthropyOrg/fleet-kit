#!/usr/bin/env python3
"""claim_history's dead-end count must not count infra failures against an item.

THE BUG (2026-09-26). claim_history.py counted every minion run against a still-open item as a
dead end, whatever its status. Five fleet:reif-priority items (philanthropy#7939/7940/7942/
7948/7950) went fleet:dead-end-blocked at count 3-4. Their runs had been SIGTERMed by a deploy
drain (rc=143), timed out before the checkpoint fix (rc=124), or shipped a Part-of PR. None
had hit a real blocker. #7939's history below is the live one, run for run.

Rule: only a minion's explicit `Blocked: #N <reason>` counts. A kill, a timeout, another infra
status, or a run that opened a checkpoint draft PR never counts.

RED on the pre-fix code: #7939's history counts 5 and reads BLOCKED. GREEN with the fix.
Plain-python test, no pytest, matching ci.yml's `python3 scripts/test_*.py`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import claim_history  # noqa: E402
import fleet_db  # noqa: E402
import run_report  # noqa: E402


def _db(recs: list[dict], d: Path):
    runs_file = d / "runs.jsonl"
    runs_file.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    conn = fleet_db.connect(d / "fleet.db")
    fleet_db.sync(conn, runs_file=runs_file)
    return conn


def _run(run_id, item_id, status, exit_code, at, **extra):
    """A started row plus its terminal row, the way run_member.sh writes every real run."""
    base = {"run_id": run_id, "member": "minion", "item_id": item_id}
    return [dict(base, status="started", _recorded_at=at),
            dict(base, status=status, exit_code=exit_code, _recorded_at=at + 60, **extra)]


def _live_7939_history(now: float) -> list[dict]:
    h = 3600
    return (
        _run("minion-item7942_7937_7938_7939_7941_7950-79476-1", "7942_7937_7938_7939_7941_7950",
             "ok", 0, now - 30 * h, outcome="Opened PR #7986, Part of #7942 only")
        + _run("minion-item7939_7938_7937_7942-4014-2", "7939_7938_7937_7942",
               "killed", 143, now - 12 * h)
        + _run("minion-item7939-36009-3", "7939", "killed", 143, now - 9 * h)
        + _run("minion-item7939-5206-4", "7939", "ok", 0, now - 6 * h,
               outcome="PR #8108 merged, Part of #7939 (phone queue cards done)")
        + _run("minion-item7939-14417-5", "7939", "timed_out", 124, now - 2 * h)
    )


def test_live_7939_history_is_not_a_dead_end() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        conn = _db(_live_7939_history(now), Path(d))
        runs = claim_history.minion_runs_for_item(conn, 7939)
        assert runs == [], f"infra/partial runs counted as dead ends: {runs}"
        assert claim_history.minion_attempts_for_item(conn, 7939) == 5
        out = subprocess.run([sys.executable, str(HERE / "claim_history.py"), "--item", "7939",
                              "--db-path", str(Path(d) / "fleet.db")],
                             capture_output=True, text=True)
        assert out.returncode == 0, (out.returncode, out.stdout, out.stderr)
        assert out.stdout.startswith("ok count=0 threshold=3 attempts=5"), out.stdout
    print("ok  #7939's live history (kills, timeout, Part-of PRs) is count=0")


def test_explicit_blocked_counts_and_blocks_at_threshold() -> None:
    now = time.time()
    recs = []
    for i in range(3):
        recs += _run(f"minion-item7948-{i}-1", "7948", "ok", 0, now - (i + 1) * 3600,
                     outcome="#7948 stays open; commented", blocked="#7948 needs Resend log access")
    recs += _run("minion-item7948-9-9", "7948", "killed", 143, now - 100)
    with tempfile.TemporaryDirectory() as d:
        conn = _db(recs, Path(d))
        runs = claim_history.minion_runs_for_item(conn, 7948)
        assert len(runs) == 3, runs
        assert claim_history.is_dead_end_blocked(runs, 7948)
        out = subprocess.run([sys.executable, str(HERE / "claim_history.py"), "--item", "7948",
                              "--db-path", str(Path(d) / "fleet.db")],
                             capture_output=True, text=True)
        assert out.returncode == 1 and "BLOCKED count=3" in out.stdout, out.stdout
    print("ok  three explicit Blocked: runs block; the kill beside them does not add a fourth")


def test_checkpoint_run_never_counts_even_if_it_says_blocked() -> None:
    now = time.time()
    recs = []
    for i in range(3):
        recs += _run(f"minion-item7937-{i}-1", "7937", "ok", 0, now - (i + 1) * 3600,
                     blocked="#7937 prod env var unset", checkpoint_pr=8152)
    with tempfile.TemporaryDirectory() as d:
        conn = _db(recs, Path(d))
        assert claim_history.minion_runs_for_item(conn, 7937) == []
    print("ok  a run that opened a checkpoint draft PR is progress, never a dead end")


def test_blocked_line_in_a_batch_only_blocks_the_items_it_names() -> None:
    blocked = "#7948 needs Resend log access"
    assert claim_history.blocked_applies_to(blocked, 7948)
    assert not claim_history.blocked_applies_to(blocked, 7950)
    # No item named: the minion said the whole pass could not go on.
    assert claim_history.blocked_applies_to("prod DATABASE_URL unreadable", 7950)
    # A bare number with no reason is not an explanation.
    assert not claim_history.blocked_applies_to("#7948", 7948)
    assert not claim_history.blocked_applies_to(None, 7948)
    for status in ("killed", "timed_out", "budget_declined", "report_lost", "paced"):
        assert not claim_history.is_dead_end_run(status, 0, blocked, None, 7948), status
    assert not claim_history.is_dead_end_run("ok", 143, blocked, None, 7948)
    assert claim_history.is_dead_end_run("ok", 0, blocked, None, 7948)
    assert claim_history.is_dead_end_run("quiet", 0, blocked, None, 7948)
    print("ok  a batch Blocked: line blocks only its named items; infra statuses never")


def test_run_report_captures_blocked_lines_and_checkpoint_pr() -> None:
    text = (
        "Report:\nBOTTOM LINE: one shipped, one blocked.\n\n"
        "- #7950 part-done in PR #8133\n"
        "- **Blocked:** #7948 needs Resend log access, filed #8084\n"
        "Blocked: #7941 spec contradicts AC2 (asked on the issue)\n"
        "Outcome: PR #8133 opened\nEvidence: `pytest` 14 passed\n"
    )
    rec = run_report.build_record(member="minion", run_id="r", kind="llm", exit_code=0,
                                  pass_text=text, usage=None, vision_required=False,
                                  item_id="7950_7948_7941", checkpoint_pr=8133)
    assert rec["blocked"] == ("#7948 needs Resend log access, filed #8084\n"
                              "#7941 spec contradicts AC2 (asked on the issue)"), rec["blocked"]
    assert rec["checkpoint_pr"] == 8133
    plain = run_report.build_record(member="minion", run_id="r", kind="llm", exit_code=0,
                                    pass_text="Outcome: PR #1 merged\nEvidence: `x`\n",
                                    usage=None, vision_required=False)
    assert plain["blocked"] is None and plain["checkpoint_pr"] is None
    print("ok  run_report records Blocked: lines and the checkpoint PR")


def main() -> int:
    try:
        test_live_7939_history_is_not_a_dead_end()
        test_explicit_blocked_counts_and_blocks_at_threshold()
        test_checkpoint_run_never_counts_even_if_it_says_blocked()
        test_blocked_line_in_a_batch_only_blocks_the_items_it_names()
        test_run_report_captures_blocked_lines_and_checkpoint_pr()
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
