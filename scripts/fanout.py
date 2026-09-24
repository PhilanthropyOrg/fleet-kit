#!/usr/bin/env python3
"""fanout — fill one hour's token allowance with work, in PERCENT OF WEEK.

THE JOB IS SELECTION, NOT COUNTING. Reif, 2026-08-26: "its more about choosing jobs to run
(that max out the availability) than it is about choosing minions to spawn." N is an output
of packing the hour, never an input. Earlier versions of this file answered "how many minions
can I afford?" and both were wrong: the first in dollars, the second still counting minions
as if every item were the same size.

WHY PERCENT. The fleet runs on a subscription, so dollars are not the constraint and never
were -- the constraint is share of the weekly token allowance. maxx is the authority, and it
already applies BOTH buffers before we see a number:

    weekly_max 0.925    holds back 7.5% of the week
    per_diem_use 0.95   holds back another 5% of the day
    per_diem_hourly_pct = the post-buffer allowance for ONE hour

So a caller spends TO per_diem_hourly_pct. It must not hold back a further reserve of its own
-- an earlier FLEET_HEADROOM_FRACTION=0.5 halved an already-buffered allowance, which is a
large part of why the fleet chronically underspent.

WHY THE HOUR IS THE UNIT. gru runs hourly precisely so each pass consumes one hour's slice.
An unspent hour does NOT roll over -- the allowance refills and the remainder is simply gone.
That makes underspending exactly as wrong as overspending, which is the opposite of how a
budget usually behaves and the single most important thing for a caller to understand.

COMPLEXITY, NOT COUNT. marie scores every item fleet:complexity-1..10 on an exponential
ladder (base 1.35, so a 10 is ~15x a 1 -- calibrated against a measured 9x p90/p10 and 22x
max/min spread across 142 real minion runs). cost_pct(item) = unit_pct * 1.35^(c-5), anchored
at 5 = the median item. Two 3s may fit an hour that one 9 would blow.

SELF-CORRECTING. `unit_pct` is not a constant to be guessed -- the caller derives it from what
passes ACTUALLY spent (calibrate() below) and re-derives it every pass. A wrong estimate is
therefore a one-pass error, not a permanent bias.

Pure: no network, no filesystem, no env reads at import. The caller supplies live numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# marie's ladder. Base and anchor must match members/marie/marie.md's Part C2 table exactly --
# if one moves without the other, gru silently mis-sizes every item it schedules.
COMPLEXITY_BASE = 1.35
COMPLEXITY_ANCHOR = 5  # the median real item; cost multiplier here is exactly 1.0
DEFAULT_COMPLEXITY = COMPLEXITY_ANCHOR  # an unlabelled item is assumed median, never free


def complexity_multiplier(c: int | None, base: float = COMPLEXITY_BASE) -> float:
    """How many median-items one complexity-c item is worth. c=5 -> 1.0, c=10 -> ~4.5, c=1 -> ~0.29."""
    if c is None:
        c = DEFAULT_COMPLEXITY
    c = max(1, min(10, int(c)))
    return base ** (c - COMPLEXITY_ANCHOR)


def calibrate(observed: list[dict], base: float = COMPLEXITY_BASE) -> float | None:
    """Derive the cost of ONE median (complexity-5) item, as % of week, from real passes.

    `observed` is [{"pct": <what it actually spent>, "complexity": <its label>}, ...]. Each
    pass is normalised by its own multiplier before averaging, so a week of mostly-easy items
    doesn't drag the unit down and make everything look cheap.

    Returns None when there is nothing usable -- the caller must then say so rather than
    invent a number. A fabricated unit silently mis-sizes every future pass.
    """
    units = [o["pct"] / complexity_multiplier(o.get("complexity"), base)
             for o in observed
             if isinstance(o.get("pct"), (int, float)) and o["pct"] > 0]
    return (sum(units) / len(units)) if units else None


def pack(items: list[dict], allowance_pct: float, unit_pct: float,
         base: float = COMPLEXITY_BASE, min_items: int = 0) -> dict:
    """Choose the item set that fills `allowance_pct` without exceeding it.

    `items` are ALREADY in the caller's priority order (marie ranks; gru does not re-rank).
    Greedy in that order, skipping an item too big for the remaining room but continuing --
    so a cheap high-priority item still gets in behind an expensive one that didn't fit. It
    does NOT reorder by size: shipping the most important work beats shipping the most work.

    `min_items` floors the selection so a thin allowance still moves something forward rather
    than idling the hour entirely -- but the caller is told, via `over_allowance`, when the
    floor pushed it past the line. Silent overspend is the one outcome this must never produce.
    """
    if unit_pct <= 0:
        raise ValueError(f"unit_pct must be > 0, got {unit_pct!r}")

    chosen, skipped, spent = [], [], 0.0
    for it in items:
        c = it.get("complexity")
        cost = unit_pct * complexity_multiplier(c, base)
        if spent + cost <= allowance_pct:
            chosen.append({**it, "est_pct": round(cost, 5)})
            spent += cost
        else:
            skipped.append({**it, "est_pct": round(cost, 5), "why": "would exceed the hour"})

    forced = 0
    while len(chosen) < min_items and skipped:
        nxt = skipped.pop(0)
        nxt.pop("why", None)
        chosen.append(nxt)
        spent += nxt["est_pct"]
        forced += 1

    return {
        "n": len(chosen),
        "chosen": chosen,
        "skipped": skipped,
        "est_spend_pct": round(spent, 5),
        "allowance_pct": round(allowance_pct, 5),
        "headroom_left_pct": round(allowance_pct - spent, 5),
        "utilization": round(spent / allowance_pct, 4) if allowance_pct > 0 else None,
        "unit_pct": round(unit_pct, 6),
        "forced_over_floor": forced,
        "over_allowance": spent > allowance_pct,
        "binding": ("nothing_claimable" if not items else
                    "min_items_floor" if forced else
                    "backlog_exhausted" if not skipped else "allowance"),
    }


def calibrate_batch_turns(observed: list[dict], base: float = COMPLEXITY_BASE) -> float | None:
    """Derive the turn cost of ONE median (complexity-5) item inside a minion BATCH pass, from
    real observed batches. Same shape as calibrate() above, different currency: turns spent on
    a batch, not % of week spent on an hour.

    `observed` is [{"turns": <real num_turns the batch pass spent>, "items": [{"complexity":c},
    ...]}, ...] -- one entry per real completed minion batch run. A batch's total complexity
    weight is the SUM of its items' multipliers (building 3 items costs roughly 3x one, same
    additive assumption pack() makes for a hard allowance; this is calibrating turns, not
    reusing pack()'s own unit, because a batch pass's real overhead -- worktree setup, one
    fetch/merge/test cycle shared across N items -- is NOT visible to the hourly cost model at
    all, only to whoever actually measures a batch run's real turns end to end).

    Returns None when there is nothing usable -- the caller must say so, never invent a number
    (same law as calibrate()).
    """
    per_item_units = []
    for o in observed:
        turns = o.get("turns")
        batch_items = o.get("items") or []
        if not isinstance(turns, (int, float)) or turns <= 0 or not batch_items:
            continue
        weight = sum(complexity_multiplier(it.get("complexity"), base) for it in batch_items)
        if weight > 0:
            per_item_units.append(turns / weight)
    return (sum(per_item_units) / len(per_item_units)) if per_item_units else None


def cluster_by_area(items: list[dict]) -> list[dict]:
    """Stable-group `items` by their "area" key (issue_cluster.area: the lane label), areas
    ordered by their highest-priority member, marie's order kept inside each area. Items with
    no area form one group of their own. Pure reordering of an ALREADY-CHOSEN set: every item
    still ships this pass, so this never trades priority for volume -- it only decides which
    items share a worktree."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for it in items:
        key = str(it.get("area") or "")
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(it)
    return [it for key in order for it in groups[key]]


def pack_batches(items: list[dict], turn_budget: float, unit_turns: float,
                 base: float = COMPLEXITY_BASE, safety_margin: float = 0.7,
                 solo_complexity_floor: int = 8, target_items: int = 0) -> dict:
    """Group an already-chosen, priority-ordered item list into minion batches, sized by real
    complexity-weighted turn cost against `turn_budget` -- NOT a fixed item count.

    2026-09-14 (Reif: "can't we just give it budget and a goal and have it innovate to maximum
    units per run" -- correctly calling out that a flat MINION_BATCH_SIZE=3 repeats the exact
    mistake fanout.py's own module docstring already named and fixed once for item COUNT vs
    item WEIGHT: "N is an output of packing the hour, never an input." A batch's size is the
    same kind of output, just packed against minion's timeout_s instead of the hourly
    allowance.

    Greedy in `items`' given order (marie's priority, gru's own selection order -- never
    reordered, same law as pack()): keep adding items to the current batch while
    `running_turns + next_item_cost <= turn_budget * safety_margin` (margin leaves headroom for
    the shared per-batch overhead -- worktree setup, one fetch/merge/test cycle across the
    whole batch -- that per-item calibration alone can't see), then close the batch and start a
    new one. An item at or above `solo_complexity_floor` is ALWAYS its own batch, even with
    room left in the current one -- a big item struggling should not risk the smaller items
    sharing its pass (minion.md's own escalation rule already says a batch's items are built
    independently, but a shared worktree means a truly pathological big item can still burn the
    whole pass's wall-clock before the small ones are ever touched).

    unit_turns must come from calibrate_batch_turns() against real history, or be explicitly
    supplied -- never guessed inline; same refusal-to-invent law as pack().

    2026-09-24 (median 2 items per run over gru's last 40 dispatches; real dino runs: 1 item
    $2.75/794s, 3 items $3.41/1312s -- the run is almost all fixed overhead):
      * AREA CLUSTERING. Items are grouped by `area` (cluster_by_area) before packing, so a
        batch is one lane's worth of related files/tests, not whatever priority order threw
        together. Budget alone closes a batch: a small area tops up the room left by the one
        before it rather than spawning its own 1-item run (that would re-create the overhead
        this exists to amortize).
      * TARGET ITEMS. `target_items` (dial FLEET_MINION_TARGET_ITEMS) raises the per-batch
        budget to what `target_items` median items cost at the CALIBRATED unit_turns --
        `max(turn_budget * safety_margin, unit_turns * target_items)`. turn_budget=0 means
        "no separate ceiling, the calibrated target IS the budget".
    """
    if unit_turns <= 0:
        raise ValueError(f"unit_turns must be > 0, got {unit_turns!r}")
    if not (0 < safety_margin <= 1):
        raise ValueError(f"safety_margin must be in (0, 1], got {safety_margin!r}")

    effective_budget = max(turn_budget * safety_margin, unit_turns * max(0, int(target_items)))
    if effective_budget <= 0:
        raise ValueError("turn_budget and target_items are both 0; nothing to size a batch against")
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_turns = 0.0

    for it in cluster_by_area(items):
        c = it.get("complexity")
        c_int = max(1, min(10, int(c))) if c is not None else DEFAULT_COMPLEXITY
        cost = unit_turns * complexity_multiplier(c, base)

        if c_int >= solo_complexity_floor:
            # Set aside, not a flush: closing the open batch here split one area's small items
            # across two runs around the big one (2026-09-24), paying the overhead twice.
            batches.append([{**it, "est_turns": round(cost, 2)}])
            continue

        if current and current_turns + cost > effective_budget:
            batches.append(current)
            current, current_turns = [], 0.0

        current.append({**it, "est_turns": round(cost, 2)})
        current_turns += cost

    if current:
        batches.append(current)

    return {
        "n_items": len(items),
        "n_batches": len(batches),
        "batches": [
            {"items": b, "est_turns": round(sum(x["est_turns"] for x in b), 2)}
            for b in batches
        ],
        "unit_turns": round(unit_turns, 3),
        "turn_budget": turn_budget,
        "effective_turn_budget": round(effective_budget, 2),
        "target_items": int(target_items),
        "n_areas": len({str(it.get("area") or "") for it in items}),
        "safety_margin": safety_margin,
        "avg_batch_size": round(len(items) / len(batches), 2) if batches else 0,
        "median_batch_size": (sorted(len(b) for b in batches)[len(batches) // 2] if batches else 0),
    }


def _run_pack(a) -> int:
    items = json.loads(sys.stdin.read() if a.items == "-" else a.items)
    unit = a.unit_pct
    if unit is None:
        if not a.observed:
            print("ERROR: pass --unit-pct or --observed; refusing to invent a unit cost",
                  file=sys.stderr)
            return 2
        unit = calibrate(json.loads(a.observed), base=a.base)
        if unit is None:
            print("ERROR: --observed had no usable pass; refusing to invent a unit cost",
                  file=sys.stderr)
            return 2

    try:
        result = pack(items, a.allowance_pct, unit, base=a.base, min_items=a.min_items)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _run_pack_batches(a) -> int:
    items = json.loads(sys.stdin.read() if a.items == "-" else a.items)
    unit = a.unit_turns
    if unit is None:
        if not a.observed:
            print("ERROR: pass --unit-turns or --observed; refusing to invent a unit cost",
                  file=sys.stderr)
            return 2
        unit = calibrate_batch_turns(json.loads(a.observed), base=a.base)
        if unit is None:
            print("ERROR: --observed had no usable batch; refusing to invent a unit cost",
                  file=sys.stderr)
            return 2

    try:
        result = pack_batches(items, a.turn_budget, unit, base=a.base,
                              safety_margin=a.safety_margin,
                              solo_complexity_floor=a.solo_complexity_floor,
                              target_items=a.target_items)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Pack one hour's token allowance with backlog work (percent of week), or "
                     "group a chosen item list into minion batches sized by real turn cost.")
    sub = ap.add_subparsers(dest="cmd")

    pack_p = sub.add_parser("pack", help="pack one hour's allowance with backlog items (default)")
    pack_p.add_argument("--allowance-pct", type=float, required=True,
                        help="this pass's share, already buffered (e.g. per_diem_hourly_pct * 0.70)")
    pack_p.add_argument("--unit-pct", type=float,
                        help="cost of one complexity-5 item as %% of week; omit to derive from --observed")
    pack_p.add_argument("--items", required=True,
                        help='JSON list in priority order: [{"number":123,"complexity":4}, ...] or "-" for stdin')
    pack_p.add_argument("--observed",
                        help='JSON list of real past passes to calibrate from: [{"pct":0.08,"complexity":5}, ...]')
    pack_p.add_argument("--min-items", type=int, default=0)
    pack_p.add_argument("--base", type=float, default=COMPLEXITY_BASE)

    batches_p = sub.add_parser(
        "batches",
        help="group an already-chosen item list into minion batches sized by real turn cost "
             "against minion's timeout_s -- NOT a fixed item count (2026-09-14, gru.md step 5)")
    batches_p.add_argument("--turn-budget", type=float, required=True,
                           help="minion's timeout_s-equivalent turn budget for one pass")
    batches_p.add_argument("--unit-turns", type=float,
                           help="turns for one complexity-5 item inside a batch pass; omit to derive from --observed")
    batches_p.add_argument("--items", required=True,
                           help='JSON list, ALREADY chosen and in priority order: '
                                '[{"number":123,"complexity":4}, ...] or "-" for stdin')
    batches_p.add_argument("--observed",
                           help='JSON list of real past BATCH passes to calibrate from: '
                                '[{"turns":38,"items":[{"complexity":4},{"complexity":3}]}, ...]')
    batches_p.add_argument("--base", type=float, default=COMPLEXITY_BASE)
    batches_p.add_argument("--safety-margin", type=float, default=0.7,
                           help="fraction of turn-budget to actually pack against, leaving "
                                "headroom for shared batch overhead (default 0.7)")
    batches_p.add_argument("--target-items", type=int,
                           default=int(os.environ.get("FLEET_MINION_TARGET_ITEMS") or 8),
                           help="raise each batch's budget to this many median items at the "
                                "calibrated unit (default: $FLEET_MINION_TARGET_ITEMS, else 8; 0 = off)")
    batches_p.add_argument("--solo-complexity-floor", type=int, default=8,
                           help="an item at or above this complexity is always its own batch (default 8)")

    # Backward compatible: no subcommand and --allowance-pct present -> old `pack` behavior,
    # unchanged interface for any existing caller that predates the `pack`/`batches` split.
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] not in ("pack", "batches", "-h", "--help"):
        argv = ["pack", *argv]
    a = ap.parse_args(argv)

    if a.cmd == "batches":
        return _run_pack_batches(a)
    return _run_pack(a)


if __name__ == "__main__":
    raise SystemExit(main())
