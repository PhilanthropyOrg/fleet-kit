#!/usr/bin/env python3
"""philanthropy#8218 "Floodgates": make the backlog buildable, batch by code area, wire HQ in.

1. area:<module> labels are the batching key (issue_cluster.area), the lane is the fallback.
2. One area:<module>, one batch per pass: an item that can't join its area's batch is DEFERRED
   (named, with a reason), never a second parallel minion in the same files, never dropped.
3. gate_drops.py intake runs the gates over EVERY open backlog item: quality:solid by default,
   needs-spec + comment for a real gap, epics untouched, one message to marie + jefe.
4. fleet:needs-prod-access is filtered like needs-human-op and messaged to hq; hq has a 30-min
   cadence; minion_checkpoint.py complete lists done + green drafts for HQ to finish.

Run: python3 scripts/test_floodgates_8218.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("FLEET_LOG_DIR", tempfile.mkdtemp())

import board_github  # noqa: E402
import fanout  # noqa: E402
import fleet_db  # noqa: E402
import fleet_msg  # noqa: E402
import gate_drops as gd  # noqa: E402
import issue_cluster  # noqa: E402
import minion_checkpoint as mc  # noqa: E402
import stale_claims  # noqa: E402

VL = "Vision-link: none (maintenance)"
GWT = "Given two issues in one module, When gru dispatches, Then they go out as one batch."
PA = "fleet:needs-prod-access"


def _issue(n, labels=(), body=f"{VL}\n\n{GWT}", title="t"):
    return {"number": n, "title": title, "labels": [{"name": x} for x in labels], "body": body,
            "comments": []}


def _ids(batch):
    return [it["number"] for it in batch["items"]]


def _all_numbers(r):
    return sorted([n for b in r["batches"] for n in _ids(b)] + [d["number"] for d in r["deferred"]])


# --- 1. area label is the key ----------------------------------------------------------------

def test_area_label_wins_over_lane() -> None:
    assert issue_cluster.area(_issue(1, ["lane:ui", "area:philanthropy:api/hq"])) == "area:philanthropy:api/hq"
    assert issue_cluster.area(_issue(2, ["lane:ui"])) == "lane:ui"
    assert issue_cluster.area(_issue(3, ["fleet:backlog"])) == ""
    gru = (HERE.parent / "members" / "gru" / "gru.md").read_text()
    assert "Also collect its `area:*` label as `\"area\"`, else its `lane:*` label" in gru
    print("ok  area(): area:<module> label wins, lane label is the fallback, else ''")


# --- 2. one area, one batch per pass ---------------------------------------------------------

def test_same_area_small_and_solo_never_two_batches() -> None:
    a = "area:philanthropy:api/hq"
    items = [{"number": 1, "complexity": 6, "area": a},
             {"number": 2, "complexity": 3, "area": a},
             {"number": 3, "complexity": 2, "area": "area:x"}]
    r = fanout.pack_batches(items, turn_budget=0, unit_turns=20, target_items=8)
    assert [_ids(b) for b in r["batches"]] == [[1], [3]], r["batches"]
    assert [d["number"] for d in r["deferred"]] == [2] and a in r["deferred"][0]["why"], r["deferred"]
    assert r["n_deferred"] == 1 and _all_numbers(r) == [1, 2, 3]
    print("ok  pack_batches: a small item whose module already has a solo batch is deferred")


def test_same_area_overflow_is_deferred_not_a_second_batch() -> None:
    items = [{"number": n, "complexity": 2, "area": "area:m"} for n in range(1, 6)]
    items.append({"number": 9, "complexity": 6, "area": "area:m"})
    r = fanout.pack_batches(items, turn_budget=0, unit_turns=20, target_items=3)
    assert [_ids(b) for b in r["batches"]] == [[1, 2, 3]], r["batches"]
    assert [d["number"] for d in r["deferred"]] == [4, 5, 9], r["deferred"]
    assert "full" in r["deferred"][0]["why"] and "solo" in r["deferred"][2]["why"]
    assert _all_numbers(r) == [1, 2, 3, 4, 5, 9], "nothing is silently dropped"
    print("ok  pack_batches: capped module overflow + its solo item go to deferred")


def test_lane_and_no_area_stay_unconstrained() -> None:
    items = [{"number": n, "complexity": 2, "area": "lane:ui"} for n in range(1, 5)]
    items += [{"number": 7, "complexity": 6, "area": "lane:ui"}, {"number": 8, "complexity": 6, "area": ""},
              {"number": 9, "complexity": 6, "area": ""}]
    r = fanout.pack_batches(items, turn_budget=0, unit_turns=20, target_items=2)
    assert r["deferred"] == [], r["deferred"]
    assert sorted(len(b["items"]) for b in r["batches"]) == [1, 1, 1, 2, 2], r["batches"]
    gru = (HERE.parent / "members" / "gru" / "gru.md").read_text()
    assert "`deferred`" in gru and "philanthropy#8218" in gru
    print("ok  pack_batches: lane-only and '' areas batch exactly as before (no deferral)")


# --- 3. intake --------------------------------------------------------------------------------

def test_intake_plan_fixes_labels_specs_and_skips() -> None:
    backlog = [_issue(10, ["fleet:backlog"]),                                  # plain: solid
               _issue(11, ["fleet:backlog"], body=VL),                         # no GWT
               _issue(12, ["fleet:backlog", "fleet:epic"], body="tracking"),   # epic
               _issue(13, ["fleet:backlog", "fleet:claimed"], body=""),        # in flight
               _issue(14, ["fleet:backlog", PA], body=""),                     # HQ's
               _issue(15, ["fleet:backlog", "quality:solid"])]                 # already fine
    p = gd.intake_plan(backlog, [_issue(14, [PA], title="read prod db")], "intake-t")
    ops = {(a["number"], a["op"], a.get("label")) for a in p["actions"]}
    assert (10, "add_label", "quality:solid") in ops and 10 in p["eligible"], p
    assert (11, "add_label", "quality:solid") in ops and (11, "add_label", gd.NEEDS_SPEC) in ops
    assert (11, "comment", None) in ops
    assert not [o for o in ops if o[0] in (12, 13, 14, 15)], ops
    assert p["eligible"] == [10, 15] and p["skipped"] == 3 and p["ask"] is None, p
    m = p["message"]
    assert tuple(m["to"]) == ("marie", "jefe") and m["items"] == [11] and "acceptance (1): #11" in m["body"]
    assert p["messages"][0]["to"] == ("hq",) and p["messages"][0]["kind"] == "prod-access"
    assert "#14 read prod db" in p["messages"][0]["body"]
    print("ok  intake: quality:solid default, needs-spec for a real gap, epics/claimed/prod untouched")


def test_intake_collapses_a_stamped_default_beside_a_scored_label() -> None:
    backlog = [_issue(20, ["fleet:backlog", "quality:solid", "quality:ship-it", gd.NEEDS_SPEC]),
               _issue(21, ["fleet:backlog", "quality:ship-it", "quality:world-class"])]
    p = gd.intake_plan(backlog, [], "intake-t")
    ops = {(a["number"], a["op"], a.get("label")) for a in p["actions"]}
    assert (20, "remove_label", "quality:solid") in ops and (20, "remove_label", gd.NEEDS_SPEC) in ops
    assert p["eligible"] == [20], p
    assert (21, "add_label", gd.NEEDS_SPEC) in ops and not [o for o in ops if o[0] == 21 and o[1] == "remove_label"]
    print("ok  intake: filer-stamped quality:solid yields to marie's score; two scored labels stay a gap")


def test_intake_resends_a_moved_gap_and_a_stale_item() -> None:
    # 2026-09-29: marie fixed #30's vision-link; it stayed labeled needs-spec and moved on to
    # acceptance, and intake never told anyone. #31 was sent 4 days ago and is still stuck.
    old = [{"body": f"{gd.MARKER} vision-link"}]
    stuck = [{"body": f"{gd.MARKER} acceptance"}]
    backlog = [_issue(30, ["fleet:backlog", "quality:solid", gd.NEEDS_SPEC], body=VL),
               _issue(31, ["fleet:backlog", "quality:solid", gd.NEEDS_SPEC], body=VL),
               _issue(32, ["fleet:backlog", "quality:solid", gd.NEEDS_SPEC], body=VL)]
    backlog[0]["comments"], backlog[1]["comments"], backlog[2]["comments"] = old, stuck, stuck
    now = 1_000_000.0
    sent = {31: now - 4 * 86400, 32: now - 3600}
    m = gd.intake_plan(backlog, [], "intake-t", last_sent=sent, now=now)["message"]
    assert m["items"] == [30, 31], m
    assert "Sent again" in m["body"] and "#31" in m["body"].split("Sent again")[1], m["body"]
    assert "#30" not in m["body"].split("Sent again")[1], m["body"]
    # No bus history (last_sent None): nothing is re-sent, only the moved gap goes out.
    assert gd.intake_plan(backlog, [], "intake-t")["message"]["items"] == [30]
    print("ok  intake: a moved gap and a 72h-stuck item reach marie; a fresh one does not")


def test_intake_parks_a_thrice_sent_low_item_and_skips_ledgers() -> None:
    # fleet-kit#1463: 8 priority-low items sent twice in 3 days, specced by nobody.
    stuck = [{"body": f"{gd.MARKER} acceptance"}]
    base = ["fleet:backlog", "quality:solid", gd.NEEDS_SPEC]
    backlog = [_issue(40, base + [gd.LOW], body=VL), _issue(41, base + [gd.LOW], body=VL),
               _issue(42, base, body=VL), _issue(43, base + [gd.LEDGER], body=VL),
               _issue(44, ["fleet:backlog", "quality:solid", gd.LOW, gd.PARKED])]
    for it in backlog[:4]:
        it["comments"] = stuck
    now = 1_000_000.0
    sent = {n: now - 4 * 86400 for n in (40, 41, 42, 43)}
    p = gd.intake_plan(backlog, [], "intake-t", last_sent=sent, now=now,
                       sends={40: 2, 41: 1, 42: 5, 43: 3})
    assert p["message"]["items"] == [41, 42], p["message"]
    assert p["parked"] == [40], p["parked"]
    ops = {(a["number"], a["op"], a.get("label")) for a in p["actions"]}
    assert (40, "add_label", gd.PARKED) in ops and not [o for o in ops if o[0] == 43], ops
    assert (44, "remove_label", gd.PARKED) in ops and 44 in p["eligible"], ops
    conn = fleet_db.connect(Path(tempfile.mkdtemp()) / "fleet.db")
    fleet_msg.send(conn, "gru", ["marie", "jefe"], "gate-drop", "k1", "b", [5, 6])
    fleet_msg.send(conn, "gru", ["marie", "jefe"], "gate-drop", "k2", "b", [5])
    assert gd.sends_by_item(conn) == {5: 2, 6: 1}, gd.sends_by_item(conn)
    print("ok  intake: a low item sent 2x is parked not re-sent; ledgers skipped; spec'd item unparked")


def test_last_sent_by_item_reads_the_bus() -> None:
    conn = fleet_db.connect(Path(tempfile.mkdtemp()) / "fleet.db")
    fleet_msg.send(conn, "gru", ["marie"], "gate-drop", "k1", "b", [5, 6])
    fleet_msg.send(conn, "gru", ["marie"], "nudge", "k2", "b", [7])
    got = gd.last_sent_by_item(conn)
    assert set(got) == {5, 6}, got
    print("ok  last_sent_by_item: gate-drop messages only, newest time per item")


def test_intake_apply_messages_marie_jefe_and_hq_once() -> None:
    db = Path(tempfile.mkdtemp()) / "fleet.db"
    calls = []

    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    backlog = [_issue(20, ["fleet:backlog", "quality:solid"], body=VL)]
    prod = [_issue(21, [PA], title="rotate the key")]
    for _ in range(2):  # a second run in the same 6h is deduped by the bus
        gd.apply(gd.intake_plan(backlog, prod, "intake-t"), None, "intake-t", run=run, db_path=str(db))
    conn = fleet_db.connect(db)
    assert [m["items"] for m in fleet_msg.inbox(conn, "marie")] == [[20]]
    assert [m["items"] for m in fleet_msg.inbox(conn, "jefe")] == [[20]]
    hq = fleet_msg.inbox(conn, "hq")
    assert len(hq) == 1 and hq[0]["kind"] == "prod-access" and hq[0]["items"] == [21], hq
    assert all(c[:3] != ["gh", "issue", "list"] for c in calls)
    print("ok  intake apply: one gate-drop to marie+jefe, one prod-access to hq, deduped")


def test_intake_cli_reads_backlog_in_small_pages() -> None:
    # jefe msg#897: the one-call read of ~570 issues failed 31 of 33 intake runs (504s).
    seen = []
    one = _issue(30, ["fleet:backlog", "quality:solid"])
    node = dict(one, createdAt="2026-10-01T00:00:00Z", labels={"nodes": one["labels"]}, comments={"nodes": one.get("comments") or []})

    def fake(cmd, timeout=60):
        seen.append(cmd)
        if cmd[1] == "api":
            page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [node]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"data": {"repository": {"issues": page}}}), stderr="")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    old = gd._gh
    gd._gh = fake
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert gd.main(["intake", "--dry-run", "--repo", "o/r"]) == 0
    finally:
        gd._gh = old
    out = json.loads(buf.getvalue())
    assert out["scanned"] == 1 and out["eligible"] == 1, out
    pages = [c for c in seen if c[1] == "api"]
    assert len(pages) == 1 and "label=fleet:backlog" in pages[0] and f"first={gd.PAGE_SIZE}" in pages[0]
    sh = (HERE / "run_gru_fanout.sh").read_text()
    assert sh.index("stale_claims.py") < sh.index("gate_drops.py\" intake") < sh.index("exec bash")
    print("ok  intake CLI: backlog read in pages; run_gru_fanout runs it after stale_claims")


def test_capped_comments_are_refetched_so_a_late_criterion_counts() -> None:
    # philanthropy#6850: gh issue list stops at the oldest 100 comments; the GWT was #102.
    old = [{"body": "Fired again", "createdAt": f"c{i:03d}"} for i in range(100)]
    full = old + [{"body": GWT, "createdAt": "c101"}]
    capped = dict(_issue(40, ["fleet:backlog", "quality:solid"]), body=VL, comments=old)
    short = dict(_issue(41, ["fleet:backlog", "quality:solid"]), comments=old[:3])
    views = []

    def fake(cmd, timeout=60):
        views.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"comments": full}), stderr="")

    items = gd.fill_capped_comments([capped, short], "o/r", run=fake)
    assert len(views) == 1 and views[0][3] == "40" and "comments" in views[0], views
    assert items[0]["comments"][-1]["body"] == GWT and len(items[1]["comments"]) == 3
    assert quality_ok(items[0]), "the criterion past the cap must now be visible to the gate"
    print("ok  capped comments: an issue at gh issue list's 100-comment cap is refetched in full")


def test_a_spec_drop_is_decided_on_github_not_the_callers_copy() -> None:
    # jefe msg#521: gru-4902 passed comments the gates could not read; 52 false needs-spec.
    good = dict(_issue(50, ["fleet:backlog", "quality:solid"]), body="Fix it",
                comments=[{"body": f"{VL}\n\n{GWT}"}])
    really_bare = dict(_issue(51, ["fleet:backlog", "quality:solid"]), body="Fix it")
    fine = _issue(52, ["fleet:backlog", "quality:solid"])
    views = []

    def fake(cmd, timeout=60):
        views.append(cmd[3])
        src = {"50": good, "51": really_bare}[cmd[3]]
        return SimpleNamespace(returncode=0, stdout=json.dumps(src), stderr="")

    items = gd.refetch_spec_drops([dict(good, comments=[]), dict(really_bare),
                                   dict(fine)], "o/r", run=fake)
    assert sorted(views) == ["50", "51"], views  # only the would-be drops are re-read
    p = gd.plan(items, "t")
    assert 50 in p["eligible"] and 52 in p["eligible"], p["eligible"]
    assert [r["number"] for r in p["dropped"] if r["action"] == "needs-spec"] == [51]
    print("ok  spec drop re-read from gh: a hand-built --items file can't false-label an item")


def quality_ok(item) -> bool:
    import quality_gate
    return quality_gate.classify_candidate(item["labels"], item["body"], item["comments"])[0]


# --- 4. HQ on the bus -------------------------------------------------------------------------

def test_needs_prod_access_is_filtered_like_needs_human_op() -> None:
    assert board_github.is_human_blocked(_issue(1, [PA]))
    assert not stale_claims.is_eligible(_issue(1, ["fleet:reif-priority", PA]))
    assert stale_claims.is_eligible(_issue(1, ["fleet:reif-priority"]))
    gru = (HERE.parent / "members" / "gru" / "gru.md").read_text()
    assert "`fleet:needs-prod-access`" in gru
    assert fleet_msg.cadence_s("hq", specs={}, env={}) == 1800.0
    assert "--me hq" in (HERE.parent / "RUNBOOK.md").read_text()
    print("ok  needs-prod-access: excluded from claims/gru like needs-human-op; hq cadence 30 min")


GREEN = [{"name": "selftest", "conclusion": "SUCCESS", "completedAt": "2026-09-26T10:00:00Z"}]
DONE_BODY = "Closes #7942\n\nDoes the thing."


def _pr(n, title="Add the thing", body=DONE_BODY, draft=True, checks=GREEN,
        branch="member/minion-item7942-1-2"):
    return {"number": n, "title": title, "body": body, "isDraft": draft, "headRefName": branch,
            "headRefOid": "abc123def456", "statusCheckRollup": checks}


def test_complete_lists_only_done_green_minion_drafts() -> None:
    red = [dict(GREEN[0], conclusion="FAILURE")]
    pending = [{"name": "selftest", "status": "IN_PROGRESS", "conclusion": ""}]
    prs = [_pr(1), _pr(2, title="WIP (minion checkpoint): #7942"), _pr(3, checks=red),
           _pr(4, checks=pending), _pr(5, draft=False), _pr(6, body="Part of #7942"),
           _pr(7, branch="hq/some-branch"), _pr(8, checks=[])]
    got = mc.complete_from(prs)
    assert [g["pr"] for g in got] == [1] and got[0]["items"] == [7942], got
    m = mc.merge_ready_message(got)
    assert m["to"] == ["hq"] and m["kind"] == "merge-ready" and m["items"] == [1]
    assert "ready --pr N --ci" in m["body"] and mc.merge_ready_message([]) is None
    print("ok  complete: only draft + Closes-every-item + green-CI minion PRs; hq message names them")


def test_complete_cli_and_ready_ci() -> None:
    state = {"pr": _pr(1, body=mc.MARKER + "\n" + DONE_BODY)}

    def fake(args, cwd, timeout=90):
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps([state["pr"]])
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps(dict(state["pr"], state="OPEN"))
        if args[:2] == ["pr", "edit"]:
            state["pr"]["body"] = args[args.index("--body") + 1]
            return 0, ""
        if args[:2] == ["pr", "ready"]:
            state["pr"]["isDraft"] = False
            return 0, ""
        return 1, "unexpected"

    old = mc._gh
    mc._gh = fake
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert mc.main(["complete", "--repo", "."]) == 0
        assert [r["pr"] for r in json.loads(buf.getvalue())] == [1]
        assert mc.ready(".", None, ci=True)["gaps"] == ["--ci needs --pr N"]
        r = mc.ready(".", 1, ci=True)
        assert r["ready"] and state["pr"]["isDraft"] is False and mc.MARKER not in state["pr"]["body"], r
        state["pr"] = _pr(2, title="WIP (minion checkpoint): #7942", checks=[])
        r = mc.ready(".", 2, ci=True)
        assert not r["ready"] and any("CI" in g for g in r["gaps"]) and state["pr"]["isDraft"], r
    finally:
        mc._gh = old
    print("ok  complete CLI prints JSON; ready --pr N --ci readies a done green draft, refuses a WIP one")


if __name__ == "__main__":
    fails = 0
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if fails else 0)
