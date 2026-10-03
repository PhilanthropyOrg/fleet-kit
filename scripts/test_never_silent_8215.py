"""philanthropy#8215: the fleet never drops work silently.

1. gru's gate drops: mechanical gap fixed, else fleet:needs-spec + one comment + one ask.
2. a gh permission denial at the end of a pass becomes an ask (deduped).
3. raw `gh issue create` is allowed for messenger/sentry and deduped by a PreToolUse hook.
4. the console has a Gate drops tile over the last 6h.

Run: python3 scripts/test_never_silent_8215.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("FLEET_LOG_DIR", tempfile.mkdtemp())

import denial_asks as da  # noqa: E402
import gate_drops as gd  # noqa: E402
import issue_create_hook as hook  # noqa: E402

VL = "Vision-link: none (maintenance)"
GWT = "Given a gru pass drops an item, When the pass ends, Then the item carries a label and a comment."


def _item(n, labels=(), body="", comments=()):
    return {"number": n, "labels": [{"name": x} for x in labels], "body": body,
            "comments": [{"body": c} for c in comments]}


def test_item_with_no_quality_label_is_fixed_and_built_this_pass() -> None:
    # philanthropy#8218 widened this from Reif's items to every non-epic item.
    for labels in (["fleet:reif-priority"], ["fleet:priority-high"]):
        p = gd.plan([_item(1, labels, f"{VL}\n\n{GWT}")], "r1")
        assert p["eligible"] == [1], p
        assert {"number": 1, "op": "add_label", "label": "quality:solid"} in p["actions"], p["actions"]
        assert p["dropped"][0]["action"] == "fixed" and p["ask"] is None, p
    print("ok  any item missing quality: label -> quality:solid, eligible now, no ask")


def test_spec_gap_gets_label_comment_and_one_ask() -> None:
    items = [_item(2, ["quality:solid", "quality:ship-it"], f"{VL}\n\n{GWT}"),  # two quality labels
             _item(3, ["quality:solid"], VL),                                # no GWT
             _item(4, ["quality:solid"], GWT),                               # no Vision-link
             _item(5, ["quality:solid"], f"{VL}\n\n{GWT}")]                  # fine
    p = gd.plan(items, "r2")
    # Two quality labels is mechanical: the stamped default comes off and it builds now.
    assert p["eligible"] == [2, 5], p
    ops = {(a["number"], a["op"], a.get("label")) for a in p["actions"]}
    assert (2, "remove_label", "quality:solid") in ops and (2, "add_label", gd.NEEDS_SPEC) not in ops, ops
    for n in (3, 4):
        assert (n, "add_label", gd.NEEDS_SPEC) in ops, (n, ops)
        assert (n, "comment", None) in ops, (n, ops)
    gaps = {d["number"]: d["gap"] for d in p["dropped"] if d["action"] == "needs-spec"}
    assert gaps == {3: "acceptance", 4: "vision-link"}, gaps
    assert p["ask"] and all(f"#{n}" in p["ask"]["why"] for n in (3, 4)), p["ask"]
    assert "#2" not in p["ask"]["why"], p["ask"]
    print("ok  spec gaps -> fleet:needs-spec + a comment naming the gap + one ask; two quality labels self-heal")


def test_second_pass_is_idempotent_and_clears_fixed_items() -> None:
    marker = gd.comment_body("acceptance", "x", "y", "r1")
    items = [_item(3, ["quality:solid", gd.NEEDS_SPEC], VL, [marker]),
             _item(6, ["quality:solid", gd.NEEDS_SPEC], f"{VL}\n\n{GWT}")]
    p = gd.plan(items, "r3")
    assert [a for a in p["actions"] if a["number"] == 3] == [], p["actions"]
    assert p["ask"] is None, "an item already labeled must not be re-asked every hour"
    assert {"number": 6, "op": "remove_label", "label": gd.NEEDS_SPEC} in p["actions"]
    print("ok  re-gating: no repeat comment/ask; a fixed item loses fleet:needs-spec")


def test_epic_ref_vision_link_inherits_the_epic_and_builds() -> None:
    # philanthropy#8538-8542: `Vision-link: #7654 (...)` was dropped as "no Vision-link line".
    item = _item(8539, ["quality:solid"], "Vision-link: #7654 (report page rebuild)\n" + GWT)
    epic = {"number": 7654, "body": "Vision-link: okr.conversion -- report page", "comments": []}
    calls = []

    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(epic), stderr="")

    parents = gd.fetch_parents([item], [], None, run=run)
    assert list(parents) == [7654] and calls[0][:4] == ["gh", "issue", "view", "7654"]
    assert gd.plan([item], "t", parents)["eligible"] == [8539]
    assert gd.plan([item], "t")["eligible"] == []
    assert gd.fetch_parents([item], [epic], None, run=None) == {7654: epic}  # no gh when known


def test_by_design_drops_are_recorded_not_labeled() -> None:
    p = gd.plan([_item(7, ["quality:solid", "fleet:epic"], f"{VL}\n\n{GWT}")], "r4")
    assert p["actions"] == [] and p["dropped"][0]["action"] == "by-design", p
    print("ok  epics / VP-pending world-class: recorded, not labeled")


def test_apply_labels_comments_logs_and_files_the_ask() -> None:
    calls = []

    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    db = Path(tempfile.mkdtemp()) / "fleet.db"
    p = gd.plan([_item(8, ["fleet:priority-high", "quality:solid"], VL)], "r5")
    out = gd.apply(p, "o/r", "r5", run=run, db_path=str(db))
    assert ["gh", "issue", "edit", "8", "--repo", "o/r", "--add-label", gd.NEEDS_SPEC] in calls, calls
    assert any(c[:4] == ["gh", "issue", "comment", "8"] for c in calls), calls
    row = sqlite3.connect(db).execute("SELECT member, status, why FROM asks WHERE id=?",
                                      (out["ask_id"],)).fetchone()
    assert row[0] == "gru" and row[1] == "open" and "#8" in row[2], row
    c = gd.count(6)
    assert c["items"] >= 1 and c.get("needs-spec", 0) >= 1, c
    print("ok  apply: gh label + comment, open ask in fleet.db, drop logged for the tile")


def test_gh_denial_becomes_one_ask_per_verb() -> None:
    result = {"permission_denials": [
        {"tool_name": "Bash", "tool_use_id": "a", "tool_input": {"command": "gh issue create --title x"}},
        {"tool_name": "Bash", "tool_use_id": "b", "tool_input": {"command": "cd /repo && gh issue create -t y"}},
        {"tool_name": "Bash", "tool_use_id": "c", "tool_input": {"command": "git push --force"}},
        {"tool_name": "Bash", "tool_use_id": "d", "tool_input": {"command": "gh pr merge 1"}}]}
    dn = da.gh_denials(result, skip_ids={"d"})
    assert [d["verb"] for d in dn] == ["gh issue create", "gh issue create"], dn
    p = da.plan_ask("sentry", "run9", dn, [])
    assert p and p["why"].startswith("permission denied: gh issue create"), p
    assert da.plan_ask("sentry", "run10", dn, [{"member": "sentry", "why": p["why"]}]) is None
    answered = {"member": "sentry", "why": p["why"], "status": "denied", "answered_at": 1000.0}
    assert da.plan_ask("sentry", "run11", dn, [answered], now=1000.0 + 86400) is None
    assert da.plan_ask("sentry", "run12", dn, [answered], now=1000.0 + 15 * 86400) is not None
    print("ok  gh denials -> one ask; open or recently answered ask not repeated; hook redirects skipped")


def test_guard_hook_block_is_not_an_ask() -> None:
    # asks #88/#89 (2026-09-28): pretest_push_hook and checkpoint_pr_hook blocks were filed as
    # "sandbox refused" asks proposing a tools.deny change for a command no deny list held.
    import hook_blocks
    d = Path(tempfile.mkdtemp())
    old = hook_blocks.LOG_DIR
    hook_blocks.LOG_DIR = d
    try:
        hook_blocks.record({"tool_use_id": "g1"}, "checkpoint_pr_hook", "Blocked: PR #8366 is a checkpoint")
        (d / "dedupe_redirects.jsonl").write_text(json.dumps({"tool_use_id": "r1"}) + "\n")
        ids = hook_blocks.blocked_ids()
        assert ids == {"g1", "r1"}, ids
        result = {"permission_denials": [
            {"tool_name": "Bash", "tool_use_id": "g1", "tool_input": {"command": "gh pr ready 8366"}},
            {"tool_name": "Bash", "tool_use_id": "x", "tool_input": {"command": "gh pr merge 2"}}]}
        dn = da.gh_denials(result, da.redirect_ids(d / "dedupe_redirects.jsonl"))
        assert [x["verb"] for x in dn] == ["gh pr merge"], dn
    finally:
        hook_blocks.LOG_DIR = old
    print("ok  a guard hook's own block never becomes a permission ask")


def test_denial_asks_cli_files_into_fleet_db() -> None:
    db = Path(tempfile.mkdtemp()) / "fleet.db"
    result = {"permission_denials": [{"tool_name": "Bash", "tool_use_id": "z",
                                      "tool_input": {"command": "gh issue create --title q"}}]}
    r = subprocess.run([sys.executable, str(HERE / "denial_asks.py"), "--member", "dont-shoot-the-messenger",
                        "--run-id", "t1", "--db-path", str(db)], input=json.dumps(result),
                       capture_output=True, text=True)
    assert r.returncode == 0 and "ask" in r.stdout, (r.stdout, r.stderr)
    n = sqlite3.connect(db).execute("SELECT count(*) FROM asks WHERE status='open'").fetchone()[0]
    assert n == 1, n
    print("ok  denial_asks.py CLI files the ask")


def test_hook_parses_and_dedupes_raw_create() -> None:
    p = hook.parse('gh issue create --repo O/R --title "Scout: foo 3 bar" --label fleet:backlog,lane:ui --body-file /tmp/x')
    assert p == {"title": "Scout: foo 3 bar", "labels": ["fleet:backlog", "lane:ui"], "repo": "O/R",
                 "body": None, "body_file": "/tmp/x"}, p
    board = [{"number": 42, "title": "Scout: foo 7 bar", "labels": [], "body": ""}]
    msg = hook.decide('gh issue create --title "Scout: foo 3 bar" --body x', list_open=lambda r: board)
    assert msg and "#42" in msg, msg
    good = "## Acceptance\n- Given a page, when I open it, then it loads.\n\nVision-link: none (maintenance)"
    assert hook.decide(f'gh issue create --title "new thing" --body "{good}"', list_open=lambda r: board) is None
    assert hook.decide('gh issue create --title "Scout: foo 3 bar" --label fleet:reif-asked',
                       list_open=lambda r: board) is None, "Reif asks are always created"

    def boom(repo):
        raise RuntimeError("gh down")
    assert hook.decide('gh issue create --title "Scout: foo 3 bar"', list_open=boom) is None
    heredoc = "gh issue create --title \"Org asks X\" --body \"$(cat <<'EOF'\nit's here\nEOF\n)\""
    assert hook.parse(heredoc)["title"] == "Org asks X", hook.parse(heredoc)
    print("ok  hook: twin blocked with its number, new/Reif/board-down allowed, heredoc parsed")


def test_hook_refuses_a_new_issue_gru_would_gate_drop() -> None:
    """jefe msg#712: philanthropy#10370 was a raw create with one prose paragraph."""
    empty = lambda r: []  # noqa: E731
    msg = hook.decide('gh issue create --title "main: test fails" --body "Moves no number (maintenance)."',
                      list_open=empty)
    assert msg and "acceptance" in msg and "vision-link" in msg, msg
    heredoc = ("gh issue create --title \"Org asks X\" --body \"$(cat <<'EOF'\nOnly prose here.\n"
               "Vision-link: none (maintenance)\nEOF\n)\"")
    msg = hook.decide(heredoc, list_open=empty)
    assert msg and "acceptance" in msg and "vision-link" not in msg, msg
    good = ("gh issue create --title \"Org asks X\" --body \"$(cat <<'EOF'\n## Acceptance\n- Given a page, "
            "when I open it, then it loads.\n\nVision-link: none (maintenance)\nEOF\n)\"")
    assert hook.decide(good, list_open=empty) is None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("just prose")
    assert hook.decide(f'gh issue create --title "x y" --body-file {fh.name}', list_open=empty)
    assert hook.decide('gh issue create --title "x y" --body-file /nonexistent/b.md', list_open=empty) is None
    assert hook.decide('gh issue create --title "x y" --body-file -', list_open=empty) is None
    assert hook.decide('gh issue create -R PhilanthropyOrg/fleet-kit --title "x y" --body prose',
                       list_open=empty) is None, "kit issues are not gru-gated"
    assert hook.decide('gh issue create --title "x y" --body prose --label fleet:reif-asked',
                       list_open=empty) is None
    print("ok  hook: a new issue gru would gate-drop is refused with the missing lines")


def test_messenger_and_sentry_may_create_issues() -> None:
    root = HERE.parent
    for name in ("dont-shoot-the-messenger", "sentry"):
        deny = json.loads((root / "members" / name / f"{name}.fleet.json").read_text())["llm"]["tools"]["deny"]
        assert "Bash(gh issue create:*)" not in deny, name
    src = (HERE / "run_member.sh").read_text()
    assert "denial_asks.py" in src, "run_member.sh never records denials"
    print("ok  messenger + sentry are not denied gh issue create; run_member.sh records denials")


def test_console_tile_registered_and_computed() -> None:
    ids = {m["id"] for m in json.loads((HERE / "metrics.json").read_text())["metrics"]}
    assert "fleet.gate_drops_6h" in ids
    assert 'out["fleet.gate_drops_6h"]' in (HERE / "fleet_view_server.py").read_text()
    log = Path(tempfile.mkdtemp()) / "g.jsonl"
    now = time.time()
    log.write_text("\n".join(json.dumps(r) for r in [
        {"ts": now - 60, "number": 1, "action": "needs-spec"},
        {"ts": now - 120, "number": 1, "action": "needs-spec"},
        {"ts": now - 7 * 3600, "number": 2, "action": "needs-spec"}]))
    assert gd.count(6, now, log) == {"items": 1, "needs-spec": 1}, gd.count(6, now, log)
    print("ok  Gate drops tile: distinct items in 6h")


def test_gru_charter_calls_gate_drops() -> None:
    md = (HERE.parent / "members" / "gru" / "gru.md").read_text()
    assert "gate_drops.py run --items" in md
    assert "python3 /fleet-kit/scripts/quality_gate.py --items" not in md
    print("ok  gru.md runs its gates through gate_drops.py")


if __name__ == "__main__":
    fails = 0
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    sys.exit(1 if fails else 0)
