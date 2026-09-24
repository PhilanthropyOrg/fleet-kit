#!/usr/bin/env python3
"""Lever 1 (2026-09-24): more items per minion run.

MEASURED. gru's last 40 minion dispatches carried a median of 2 items. Real dino minion runs
(7 days) show why that is the expensive shape: a 1-item run cost a median $2.75 / 794s and a
3-item run $3.41 / 1312s -- nearly all of a run is fixed overhead (worktree, fetch, test, PR),
so items per run is the lever. Three defects kept it at ~2:

  1. cost_bridge.to_observed booked a whole BATCH run ("7473_7474_7475") as ONE median item, so
     the calibrated unit cost was really a per-RUN cost and gru's hour pack chose ~one item per
     recent run -- a fixed point at the current batch size, with no pressure upward.
  2. pack_batches grouped in priority order only, so a batch mixed unrelated lanes and paid the
     full context-switch overhead per item.
  3. There was no target: nothing asked for 8.

RED without the fix: pack_batches has no area clustering / target_items, to_observed does not
split batch runs, fleet_metrics has no items_per_run. GREEN with it. Plain python, no pytest.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cost_bridge  # noqa: E402
import fanout  # noqa: E402
import fleet_metrics  # noqa: E402


def _ids(batch):
    return [it["number"] for it in batch["items"]]


def test_pack_batches_clusters_by_area() -> None:
    items = [{"number": n, "complexity": 5, "area": a} for n, a in
             [(1, "lane:ui"), (2, "lane:devops"), (3, "lane:ui"), (4, "lane:devops"),
              (5, "lane:ui"), (6, "lane:devops")]]
    r = fanout.pack_batches(items, turn_budget=30 * 3 / 0.7, unit_turns=30)
    got = [_ids(b) for b in r["batches"]]
    assert got == [[1, 3, 5], [2, 4, 6]], f"batches mix areas: {got}"
    assert r["n_areas"] == 2, r
    print("ok  pack_batches: same-area items share a batch, highest-priority area first")


def test_pack_batches_target_items_raises_budget_from_calibration() -> None:
    items = [{"number": n, "complexity": 5, "area": "lane:fleet"} for n in range(10)]
    # Old shape: the budget only fits one median item per batch -> 10 runs for 10 items.
    r = fanout.pack_batches(items, turn_budget=60, unit_turns=30, target_items=8)
    sizes = [len(b["items"]) for b in r["batches"]]
    assert sizes == [8, 2], f"target_items=8 should size batches to 8 median items, got {sizes}"
    assert r["effective_turn_budget"] == 240, r
    # No turn budget at all: the calibrated unit * target IS the per-batch budget.
    r = fanout.pack_batches(items, turn_budget=0, unit_turns=30, target_items=8)
    assert [len(b["items"]) for b in r["batches"]] == [8, 2], r
    print("ok  pack_batches: target_items sizes batches from the calibrated unit, not a guess")


def test_pack_batches_big_item_still_solo_and_priority_kept_inside_area() -> None:
    items = [{"number": 1, "complexity": 9, "area": "lane:ui"},
             {"number": 2, "complexity": 3, "area": "lane:ui"},
             {"number": 3, "complexity": 2, "area": "lane:ui"}]
    r = fanout.pack_batches(items, turn_budget=0, unit_turns=20, target_items=8)
    assert [_ids(b) for b in r["batches"]] == [[1], [2, 3]], r["batches"]
    print("ok  pack_batches: a complexity>=8 item stays solo; order inside an area is marie's")


def test_cost_bridge_splits_a_batch_run_across_its_items() -> None:
    runs = [{"item_id": "10_11_12", "cost_usd": 3.0}, {"item_id": "20", "cost_usd": 1.0}]
    obs = cost_bridge.to_observed(runs, allowance_pct=1.0)
    assert len(obs) == 4, f"a 3-item batch must calibrate as 3 items, got {obs}"
    unit = fanout.calibrate(obs)
    assert abs(unit - 0.25) < 1e-9, f"unit should be 1.0/4 items = 0.25, got {unit}"
    print("ok  cost_bridge: a batch run's spend is split over its items (unit is per ITEM)")


def test_metric_items_per_run_is_the_real_median() -> None:
    now = 1_000_000.0
    rows = [{"member": "minion", "status": "ok", "ts": now - 60 * i, "item_id": iid}
            for i, iid in enumerate(["1", "1_2", "1_2_3", "1_2_3_4_5_6_7_8", "1_2"])]
    rows.append({"member": "minion", "status": "paced", "ts": now - 10, "item_id": "9"})
    v = fleet_metrics.compute("items_per_run:minion", rows, at=now, hours=24)
    assert v == statistics.median([1, 2, 3, 8, 2]), v
    print("ok  fleet_metrics items_per_run:minion = median items of executed runs")


if __name__ == "__main__":
    fails = 0
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if fails else 0)
