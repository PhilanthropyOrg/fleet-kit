"""gate_drops.candidates hands gru every tier in pack order, so it never stops after high (gru underspend, 10-05)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_drops  # noqa: E402


def _item(n, created, *labels):
    return {"number": n, "createdAt": created, "labels": [{"name": lb} for lb in labels]}


def test_every_tier_comes_back_high_then_medium_then_low_then_unranked_oldest_first():
    items = [
        _item(1, "2026-10-01", "fleet:backlog", "fleet:priority-low"),
        _item(2, "2026-10-03", "fleet:backlog", "fleet:priority-medium"),
        _item(3, "2026-10-02", "fleet:backlog"),
        _item(4, "2026-10-04", "fleet:backlog", "fleet:priority-high"),
        _item(5, "2026-09-01", "fleet:backlog", "fleet:priority-medium"),
        _item(6, "2026-10-05", "fleet:backlog", "fleet:priority-high"),
    ]
    assert [i["number"] for i in gate_drops.candidates(items)] == [4, 6, 5, 2, 1, 3]


def test_claimed_human_op_prod_access_and_parked_items_are_left_out():
    items = [
        _item(1, "2026-10-01", "fleet:backlog", "fleet:priority-high", gate_drops.LABEL_CLAIMED),
        _item(2, "2026-10-01", "fleet:backlog", "fleet:priority-high", gate_drops.LABEL_PROD_ACCESS),
        _item(3, "2026-10-01", "fleet:backlog", "fleet:priority-low", gate_drops.PARKED),
        _item(4, "2026-10-01", "fleet:backlog", "fleet:priority-medium"),
    ]
    assert [i["number"] for i in gate_drops.candidates(items)] == [4]


def test_the_cli_writes_the_file_and_counts_each_tier(tmp_path, monkeypatch, capsys):
    import json
    backlog = [_item(7, "2026-10-01", "fleet:priority-medium"), _item(8, "2026-10-02", "fleet:priority-high")]
    monkeypatch.setattr(gate_drops, "list_open", lambda *a, **k: backlog)
    monkeypatch.setattr(gate_drops, "fill_capped_comments", lambda items, repo: items)
    monkeypatch.setattr(gate_drops, "hydrate", lambda nums, repo: [dict(_item(n, ""), body="b") for n in nums])
    out = tmp_path / "c.json"
    assert gate_drops.main(["candidates", "--out", str(out), "--first", "1"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["numbers"] == [8] and got["beyond_first"] == 1
    assert got["by_tier"] == {"high": 1, "medium": 1, "low": 0, "unranked": 0}
    assert [i["body"] for i in json.loads(out.read_text())] == ["b"]


def test_fresh_items_fill_the_read_before_labelled_dead_ends():
    dead = [_item(n, f"2026-09-0{n}", "fleet:priority-high", gate_drops.DEAD_END) for n in range(1, 6)]
    fresh = [_item(n, "2026-10-01", "fleet:priority-medium") for n in (10, 11, 12)]
    todo = gate_drops.candidates(dead + fresh)
    assert [i["number"] for i in gate_drops.read_slice(todo, 5, recheck=2)] == [10, 11, 12, 1, 2]
    # few fresh items: dead ends fill the rest; many fresh: only the recheck share is dead ends
    assert [i["number"] for i in gate_drops.read_slice(todo, 8, recheck=2)] == [10, 11, 12, 1, 2, 3, 4, 5]
    assert [i["number"] for i in gate_drops.read_slice(todo, 3, recheck=1)] == [10, 11, 1]


def test_hydrate_keeps_order_and_leaves_out_an_unreadable_item():
    import json
    import subprocess

    def run(cmd):
        n = int(cmd[3])
        if n == 2:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 504")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"number": n}), stderr="")
    assert [i["number"] for i in gate_drops.hydrate([3, 2, 1], None, run=run)] == [3, 1]


def test_items_a_pr_already_builds_are_not_candidates():
    # 2026-10-08: the hour's one minion slot went three times to items already built: #11634
    # (open PR #11741 names it), #11712/#11713 (PR #11739 merged 20 min earlier, waiting on
    # deploy and the prod walk before the issue closes). Each minion read the PRs, built
    # nothing and burned the slot. An open PR or one merged in the last day holds the item.
    prs = [
        {"number": 11741, "state": "OPEN", "headRefName": "member/minion-item11646_11635_11634-32-1", "body": ""},
        {"number": 11739, "state": "MERGED", "headRefName": "x", "body": "Part of #11712\nPart of #11713",
         "mergedAt": "2026-10-08T07:46:28Z"},
        {"number": 11000, "state": "MERGED", "headRefName": "member/minion-item9000-1-1", "body": "",
         "mergedAt": "2026-10-05T07:46:28Z"},
        {"number": 11001, "state": "CLOSED", "headRefName": "member/minion-item9001-1-1", "body": ""},
    ]
    import calendar
    import time
    now = calendar.timegm(time.strptime("2026-10-08T08:30:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    built = gate_drops.built_by_prs(prs, now)
    assert set(built) == {11634, 11635, 11646, 11712, 11713}
    assert built[11712] == "PR #11739 merged 2026-10-08 07:46Z"
    assert built[11634] == "PR #11741 open"
    items = [_item(n, "2026-10-01", "fleet:backlog", "fleet:priority-high") for n in (11634, 11712, 9000, 9001, 10950)]
    assert [i["number"] for i in gate_drops.candidates(items, built=built)] == [9000, 9001, 10950]
