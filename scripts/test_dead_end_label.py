#!/usr/bin/env python3
"""philanthropy#5934: gru's dead-end guard must be visible on the board, not silent.

THE BUG. claim_history.is_dead_end_blocked told gru.md to drop a chronically-reclaimed item
from its candidate set, but nothing wrote that anywhere -- no label, no comment. Thirty
fleet:priority-high items sat this way on 2026-09-14, including this tracking issue itself.

RED without the fix: test_plan_block_adds_label_and_comment_when_absent fails (plan_block
returns []) because dead_end_label.py does not exist / has no side-effecting plan. GREEN with
it. Plain-python test, no pytest -- matches ci.yml, which runs `python3 scripts/test_*.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dead_end_label as del_  # noqa: E402


def test_plan_block_adds_label_and_comment_when_absent() -> None:
    issue = {"number": 2195, "labels": [], "comments": []}
    cmds = del_.plan_block(issue, count=5, threshold=3, run_id="gru-123")
    assert any(c[:2] == ["gh", "issue"] and "--add-label" in c and del_.LABEL_DEAD_END_BLOCKED in c
               for c in cmds), f"no add-label command: {cmds}"
    assert any(c[:2] == ["gh", "issue"] and "comment" in c and "count=5" in " ".join(c)
               for c in cmds), f"no comment command: {cmds}"
    print("ok  plan_block adds the label and posts a comment on a fresh block")


def test_plan_block_idempotent_same_count_when_already_labeled() -> None:
    issue = {
        "number": 2195,
        "labels": [{"name": del_.LABEL_DEAD_END_BLOCKED}],
        "comments": [{"body": del_.block_comment_body(5, 3, "gru-earlier")}],
    }
    cmds = del_.plan_block(issue, count=5, threshold=3, run_id="gru-later")
    assert cmds == [], f"expected no-op (already labeled, same count), got {cmds}"
    print("ok  plan_block is idempotent while labeled and the count hasn't changed")


def test_plan_block_new_comment_when_count_rises_while_labeled() -> None:
    issue = {
        "number": 2195,
        "labels": [{"name": del_.LABEL_DEAD_END_BLOCKED}],
        "comments": [{"body": del_.block_comment_body(3, 3, "gru-earlier")}],
    }
    cmds = del_.plan_block(issue, count=4, threshold=3, run_id="gru-later")
    assert len(cmds) == 1 and "comment" in cmds[0], f"expected one fresh comment, got {cmds}"
    assert "count=4" in " ".join(cmds[0]), cmds
    assert not any("--add-label" in c for c in cmds), f"label already present, must not re-add: {cmds}"
    print("ok  plan_block posts a fresh comment (no relabel) when the count moves while blocked")


def test_plan_block_fresh_comment_after_manual_clear() -> None:
    """criterion 3: a human removes the label by hand; count is unchanged at re-check time ->
    next pass must re-block with a NEW comment, not stay silently cleared."""
    issue = {
        "number": 2195,
        "labels": [],
        "comments": [{"body": del_.block_comment_body(5, 3, "gru-earlier")}],
    }
    cmds = del_.plan_block(issue, count=5, threshold=3, run_id="gru-retry")
    assert any("--add-label" in c for c in cmds), f"expected re-add of label: {cmds}"
    assert any("comment" in c and "gru-retry" in " ".join(c) for c in cmds), \
        f"expected a fresh comment naming the new run, not silence: {cmds}"
    print("ok  a manual clear is a real retry: re-blocked with a fresh comment")


def test_plan_unblock_removes_label() -> None:
    issue = {"number": 2195, "labels": [{"name": del_.LABEL_DEAD_END_BLOCKED}]}
    cmds = del_.plan_unblock(issue)
    assert cmds == [["gh", "issue", "edit", "2195", "--remove-label", del_.LABEL_DEAD_END_BLOCKED]], cmds
    print("ok  plan_unblock strips the label once the count ages back under threshold")


def test_plan_unblock_noop_when_not_labeled() -> None:
    issue = {"number": 2195, "labels": []}
    assert del_.plan_unblock(issue) == [], "unblocking an unlabeled issue must be a no-op"
    print("ok  plan_unblock is a no-op on an issue that was never labeled")


def main() -> int:
    try:
        test_plan_block_adds_label_and_comment_when_absent()
        test_plan_block_idempotent_same_count_when_already_labeled()
        test_plan_block_new_comment_when_count_rises_while_labeled()
        test_plan_block_fresh_comment_after_manual_clear()
        test_plan_unblock_removes_label()
        test_plan_unblock_noop_when_not_labeled()
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
