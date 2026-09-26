#!/usr/bin/env python3
"""A minion batch must fit inside the minion's timeout (2026-09-26).

INCIDENT. gru's 02:37 UTC 09-26 pass packed #7938 #7939 #7941 #7950 -- four complexity-5
fleet:reif-priority items -- into ONE minion, on an instance with FLEET_MINION_TARGET_ITEMS=3
(gru typed `--target-items 8`). It hit rc=124 at 04:09 with no PR; ~90 min lost, all four back
to unclaimed. Same shape at 09:19 UTC 09-25. pack_batches treated target_items as a budget
FLOOR, never a count cap, and only complexity >= 8 went solo.

RED without the fix: four c5 items land in one batch; the env ceiling is ignored; no weight
ceiling exists. GREEN with it. Plain python, no pytest.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fanout  # noqa: E402

INCIDENT = [{"number": n, "complexity": 5, "area": ""} for n in (7938, 7939, 7941, 7950)]


def _ids(r):
    return [[it["number"] for it in b["items"]] for b in r["batches"]]


def test_incident_batch_splits_one_per_minion() -> None:
    # Exactly the 02:37 call: --turn-budget 0 --target-items 8, a calibrated unit.
    r = fanout.pack_batches(INCIDENT, turn_budget=0, unit_turns=30, target_items=8)
    assert _ids(r) == [[7938], [7939], [7941], [7950]], _ids(r)
    print("ok  the 09-26 02:37 batch (four c5 items) packs as four parallel minions")


def test_target_items_is_a_hard_count_cap() -> None:
    small = [{"number": n, "complexity": 1, "area": "lane:x"} for n in range(7)]
    r = fanout.pack_batches(small, turn_budget=10_000, unit_turns=1, target_items=3)
    assert [len(b) for b in _ids(r)] == [3, 3, 1], _ids(r)
    print("ok  target_items caps items per batch even when the turn budget has room")


def test_timeout_ceiling_limits_summed_complexity() -> None:
    # 5400s * 0.7 / 1800s = 2.1 median items of wall clock. c4 = 0.74 each -> 2 per batch.
    c4 = [{"number": n, "complexity": 4, "area": "lane:x"} for n in range(5)]
    r = fanout.pack_batches(c4, turn_budget=0, unit_turns=30, target_items=8,
                            timeout_s=5400, unit_seconds=1800)
    assert r["max_batch_weight"] == 2.1, r
    assert [len(b) for b in _ids(r)] == [2, 2, 1], _ids(r)
    for b in r["batches"]:
        w = sum(fanout.complexity_multiplier(it["complexity"]) for it in b["items"])
        assert w <= 2.1, (b, w)
    print("ok  a batch's summed complexity never exceeds what fits minion's timeout")


def test_item_bigger_than_timeout_ships_solo_and_flagged() -> None:
    items = [{"number": 1, "complexity": 9, "area": ""}, {"number": 2, "complexity": 2, "area": ""}]
    r = fanout.pack_batches(items, turn_budget=0, unit_turns=30, target_items=3,
                            timeout_s=5400, unit_seconds=1800, solo_complexity_floor=10)
    assert _ids(r) == [[1], [2]], _ids(r)
    assert r["over_timeout"] == [1], r
    print("ok  an item heavier than the timeout alone is solo and named in over_timeout")


def _cli(env_extra: dict) -> dict:
    env = {**os.environ, **env_extra}
    out = subprocess.run(
        [sys.executable, str(HERE / "fanout.py"), "batches", "--turn-budget", "0",
         "--target-items", "8", "--unit-turns", "30", "--items", json.dumps(
             [{"number": n, "complexity": 2, "area": ""} for n in range(6)])],
        capture_output=True, text=True, env=env, check=True).stdout
    return json.loads(out)


def test_cli_env_target_items_is_a_ceiling_on_the_flag() -> None:
    r = _cli({"FLEET_MINION_TARGET_ITEMS": "3", "FLEET_MINION_TIMEOUT_S": "0"})
    assert r["target_items"] == 3, r
    assert [len(b["items"]) for b in r["batches"]] == [3, 3], r["batches"]
    print("ok  CLI: $FLEET_MINION_TARGET_ITEMS=3 beats a typed --target-items 8")


def test_cli_defaults_solo_5_and_reads_minion_timeout() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_MINION_")}
    out = subprocess.run(
        [sys.executable, str(HERE / "fanout.py"), "batches", "--turn-budget", "0",
         "--unit-turns", "30", "--items", json.dumps(INCIDENT)],
        capture_output=True, text=True, env=env, check=True).stdout
    r = json.loads(out)
    spec = json.loads((HERE.parent / "members/minion/minion.fleet.json").read_text())
    assert r["timeout_s"] == float(spec["timeout_s"]), r
    assert r["solo_complexity_floor"] == 5 and r["n_batches"] == 4, r
    print("ok  CLI: solo floor defaults to 5 and the ceiling reads minion's own timeout_s")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
