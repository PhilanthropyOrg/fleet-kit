#!/usr/bin/env python3
"""Tests for queue_watch's pure core (decide), with fake queue data.

The shapes come from the app repo's merge queue on 2026-11-03: the head entry sat in
AWAITING_CHECKS for 6.5 hours with no merge_group run ever created for it, an UNMERGEABLE
entry at position 2 held the batch, and nothing merged from about 7:20 AM Central.

Run: python3 scripts/test_queue_watch.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import queue_watch as qw  # noqa: E402

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)

ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except AssertionError as exc:
        fail.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


def entry(pr, position, state="AWAITING_CHECKS", *, enq_min_ago=120, head="h", base="b"):
    return {
        "pr": pr, "pr_id": f"PR_{pr}", "position": position, "state": state,
        "enqueued_at": NOW - enq_min_ago * MIN,
        "head_oid": f"{head}{pr}" if head else None, "base_oid": f"{base}{pr}",
        "head_ref_oid": f"ref{pr}",
    }


def run_for(e, created_min_ago=5):
    return {"head_branch": f"gh-readonly-queue/main/pr-{e['pr']}-{e['base_oid']}",
            "head_sha": e["head_oid"], "created_at": NOW - created_min_ago * MIN}


def kinds(actions):
    return [(a["kind"], a["pr"]) for a in actions if a["kind"] != "alarm"]


def recent_merge():
    return NOW - 10 * MIN


def seen_stuck(e, min_ago):
    return {"stuck_seen": {f"{e['pr']}:{e['head_oid']}": (NOW - min_ago * MIN).timestamp()}}


# --- fine -------------------------------------------------------------------------------------

def t_fine_head_has_run():
    head = entry(15313, 1)
    acts, _ = qw.decide([head, entry(15133, 2)], [run_for(head)], recent_merge(), {}, NOW)
    assert acts == [], acts


def t_fine_head_running_by_branch_name_only():
    head = entry(15313, 1)
    r = run_for(head)
    r["head_sha"] = "other"
    acts, _ = qw.decide([head], [r], recent_merge(), seen_stuck(head, 60), NOW)
    assert acts == [], acts


def t_fine_young_head_not_kicked():
    head = entry(15313, 1, enq_min_ago=10)
    acts, _ = qw.decide([head], [], recent_merge(), seen_stuck(head, 60), NOW)
    assert acts == [], acts


def t_first_sight_waits():
    # Old enqueuedAt but this group was only just seen without a run: wait, remember it.
    head = entry(15313, 1)
    acts, st = qw.decide([head], [], recent_merge(), {}, NOW)
    assert acts == [], acts
    assert st["stuck_seen"], st


def t_old_run_from_before_enqueue_does_not_count():
    head = entry(15295, 1, enq_min_ago=60)
    acts, _ = qw.decide([head], [run_for(head, created_min_ago=90)], recent_merge(),
                        seen_stuck(head, 20), NOW)
    assert kinds(acts) == [("kick", 15295)], acts


# --- stuck head -------------------------------------------------------------------------------

def t_stuck_head_kicked():
    head = entry(15295, 1, enq_min_ago=390)
    other = entry(15316, 2)
    acts, st = qw.decide([head, other], [run_for(other)], recent_merge(), seen_stuck(head, 16), NOW)
    assert kinds(acts) == [("kick", 15295)], acts
    assert len(st["kicks"]["15295"]) == 1, st


def t_stuck_head_with_null_head_commit():
    head = entry(15295, 1, head=None)
    acts, _ = qw.decide([head], [], recent_merge(),
                        {"stuck_seen": {"15295:none": (NOW - 20 * MIN).timestamp()}}, NOW)
    assert kinds(acts) == [("kick", 15295)], acts


def t_not_head_not_kicked():
    e = entry(15295, 2)
    acts, _ = qw.decide([entry(1, 1, "MERGEABLE"), e], [], recent_merge(), seen_stuck(e, 60), NOW)
    assert ("kick", 15295) not in kinds(acts), acts


# --- kick limit -------------------------------------------------------------------------------

def t_third_time_dequeued_and_commented_once():
    head = entry(15295, 1)
    st = seen_stuck(head, 20)
    st["kicks"] = {"15295": [(NOW - 100 * MIN).timestamp(), (NOW - 50 * MIN).timestamp()]}
    acts, st2 = qw.decide([head], [], recent_merge(), st, NOW)
    assert kinds(acts) == [("dequeue", 15295)], acts
    assert acts[0]["comment"] == qw.STUCK_COMMENT, acts
    # Next pass (the arm sweep put it back): dequeued again, no second comment.
    st2["stuck_seen"] = {"15295:h15295": (NOW - 20 * MIN).timestamp()}
    acts3, _ = qw.decide([head], [], recent_merge(), st2, NOW + 5 * MIN)
    assert kinds(acts3) == [("dequeue", 15295)], acts3
    assert acts3[0]["comment"] is None, acts3


def t_old_kicks_expire_after_6h():
    head = entry(15295, 1)
    st = seen_stuck(head, 20)
    st["kicks"] = {"15295": [(NOW - 7 * 60 * MIN).timestamp(), (NOW - 50 * MIN).timestamp()]}
    acts, _ = qw.decide([head], [], recent_merge(), st, NOW)
    assert kinds(acts) == [("kick", 15295)], acts


# --- unmergeable ------------------------------------------------------------------------------

def t_unmergeable_any_position_dequeued_with_comment():
    head = entry(15313, 1)
    bad = entry(15316, 7, "UNMERGEABLE", head=None)
    st = {"unmergeable_seen": {"15316": (NOW - 6 * MIN).timestamp()}}
    acts, st2 = qw.decide([head, bad], [run_for(head)], recent_merge(), st, NOW)
    assert kinds(acts) == [("dequeue", 15316)], acts
    assert acts[0]["comment"] == qw.CONFLICT_COMMENT, acts
    acts2, _ = qw.decide([head, bad], [run_for(head)], recent_merge(),
                         {**st2, "unmergeable_seen": {"15316": (NOW - 6 * MIN).timestamp()}}, NOW)
    assert acts2[0]["comment"] is None, acts2


def t_unmergeable_first_sight_waits_one_pass():
    bad = entry(15316, 2, "UNMERGEABLE")
    acts, st = qw.decide([entry(1, 1, "MERGEABLE"), bad], [], recent_merge(), {}, NOW)
    assert acts == [], acts
    assert "15316" in st["unmergeable_seen"], st


# --- no-merge alarm ---------------------------------------------------------------------------

def t_alarm_once_when_no_merge_90m_and_queue_3():
    q = [entry(1, 1, "MERGEABLE"), entry(2, 2, "QUEUED"), entry(3, 3, "QUEUED")]
    acts, st = qw.decide(q, [], NOW - 95 * MIN, {}, NOW)
    assert [a["kind"] for a in acts] == ["alarm"], acts
    acts2, st2 = qw.decide(q, [], NOW - 100 * MIN, st, NOW + 5 * MIN)
    assert acts2 == [], acts2
    _, st3 = qw.decide(q, [], NOW, st2, NOW + 10 * MIN)
    assert not st3.get("alarm"), st3


def t_no_alarm_short_queue():
    q = [entry(1, 1, "MERGEABLE"), entry(2, 2, "QUEUED")]
    acts, _ = qw.decide(q, [], NOW - 300 * MIN, {}, NOW)
    assert acts == [], acts


for _name, _fn in list(globals().items()):
    if _name.startswith("t_") and callable(_fn):
        check(_name, _fn)

for name in ok:
    print(f"ok   {name}")
for name, why in fail:
    print(f"FAIL {name}: {why}")
print(f"{len(ok)} passed, {len(fail)} failed")
sys.exit(1 if fail else 0)
