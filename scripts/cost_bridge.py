#!/usr/bin/env python3
"""cost_bridge -- converts real fleet.db cost_usd into fanout.py's %-of-week currency.

THE GAP THIS CLOSES (gh#4020 / fleet-kit#260). fanout.py's own docstring already promises
`unit_pct` is "SELF-CORRECTING... the caller derives it from what passes ACTUALLY spent" --
but nothing in the repo ever built that derivation, so every gru pass since at least
2026-08-30 called it with a hand-typed `--unit-pct 0.05` guess instead. fleet.db has real
`cost_usd` sitting in it (20+ minion runs at any given time); this module is the missing
function that turns that into the `{"pct": ..., "complexity": ...}` shape `fanout.py
--observed` expects.

THE DERIVATION. `allowance_pct` is the ONE real %-of-week quantity a gru pass already reads,
once, per pass (maxx_reader.get_headroom's per_diem_hourly_pct, already buffered -- see
gru.md step 1). This module does not read maxx again -- fleet-kit#260's own PRD ruled out a
second meter read per run (a before/after read around every minion would be more accurate but
adds a network round-trip to every single run, which conflicts with that PRD's AC5 and with
maxx_reader.py's fail-open ethos). Instead, `to_observed()` distributes that ONE already-known
allowance_pct across a set of real runs' `cost_usd`, proportional to each run's share of the
group's total spend: a run that cost twice another's dollars is assumed to have used twice its
share of the hour's real allowance. Wrong in any single pass, self-correcting like every other
number `fanout.py` computes -- `--observed` gets rebuilt fresh from real spend every pass, so a
one-pass miss is not a permanent bias.

Pure core (`to_observed`), thin DB seam (`recent_minion_costs`), CLI (`main`) -- same split as
fleet_db.py's spend()/query_runs() and fanout.py's calibrate()/main().
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fanout  # noqa: E402
import fleet_db  # noqa: E402

# 2026-09-25 COLD START. to_observed() books the window's whole spend as exactly one allowance,
# so its unit is allowance / items-in-the-window. One recent run made the whole allowance ONE
# item's cost and fanout.py packed n=1 whatever the headroom (live gru pass 12:48 UTC: OBS=
# [{"pct": 36.0}], 9 eligible fleet:reif-priority items, 1 built). A thin window measures how
# idle the fleet was, not what an item costs -- and it is a fixed point: n=1 this hour leaves one
# run in the window for the next. So a thin window falls back to 30 days of real minion passes.
WARM_MIN_RUNS = 5            # fewer usable runs than this in the window => cold start
HISTORY_HOURS = 30 * 24      # the cold-start reference window
BUSY_HOUR_PERCENTILE = 0.9   # "a full hour" = what minions spent in the fleet's p90 hour
MIN_ITEMS_PER_HOUR = 4       # on EVERY path, a median item costs at most allowance / this


def to_observed(runs: list[dict], allowance_pct: float,
                complexity_by_item: dict[str, int] | None = None) -> list[dict]:
    """[{"item_id":.., "cost_usd":..}, ...] + this pass's allowance_pct -> fanout.py's
    --observed shape: [{"pct": float, "complexity": int}, ...].

    `complexity_by_item` maps an item's id (as a string, matching fleet_db's item_id column)
    to its `fleet:complexity-<1-10>` label. An item missing from the map is treated as
    fanout.DEFAULT_COMPLEXITY (median), never free -- same convention gru.md step 2 and
    fanout.py's own DEFAULT_COMPLEXITY already use for an unlabelled item.

    Returns [] when there is nothing usable (no positive cost_usd, or allowance_pct <= 0) --
    same "refuse to invent" discipline as fanout.calibrate(): the caller (fanout.py, fed this
    output as --observed) already errors loudly on an empty/unusable --observed rather than
    silently packing against a fabricated number, so this function does not need to duplicate
    that refusal.
    """
    complexity_by_item = complexity_by_item or {}
    usable = [r for r in runs
             if isinstance(r.get("cost_usd"), (int, float)) and r["cost_usd"] > 0]
    total_cost = sum(r["cost_usd"] for r in usable)
    if not usable or allowance_pct <= 0 or total_cost <= 0:
        return []
    # 2026-09-24: a batched run's item_id is underscore-joined ("7473_7474_7475", same as
    # to_batch_observed below). Booking it as ONE median item made the calibrated unit a
    # per-RUN cost, so gru's hour pack chose ~one item per recent run -- a fixed point at the
    # current batch size (median 2). Split each run's share over its items by complexity weight.
    out = []
    for r in usable:
        run_pct = allowance_pct * (r["cost_usd"] / total_cost)
        ids = [n for n in str(r.get("item_id") or "").split("_") if n] or [""]
        cx = [complexity_by_item.get(n, fanout.DEFAULT_COMPLEXITY) for n in ids]
        weights = [fanout.complexity_multiplier(c) for c in cx]
        total_w = sum(weights)
        out += [{"pct": run_pct * w / total_w, "complexity": c} for c, w in zip(cx, weights)]
    return out


def _unit_usd_per_item(run: dict, complexity_by_item: dict[str, int]) -> list[float]:
    """One run's cost, as the $ cost of a median item, once per item it built (a batched run's
    item_id is underscore-joined; its cost splits by complexity weight, same as to_observed)."""
    ids = [n for n in str(run.get("item_id") or "").split("_") if n] or [""]
    total_w = sum(fanout.complexity_multiplier(complexity_by_item.get(n, fanout.DEFAULT_COMPLEXITY))
                  for n in ids)
    return [run["cost_usd"] / total_w] * len(ids)


def cold_start_observed(history_runs: list[dict], allowance_pct: float,
                        complexity_by_item: dict[str, int] | None = None) -> list[dict]:
    """--observed for a thin window, from the last HISTORY_HOURS of real minion passes
    ([{"item_id", "cost_usd", "recorded_at"}, ...], one per run).

    unit_pct = allowance_pct * median_item_usd / busy_hour_usd: the median item's real $ cost
    over what minions spent in the fleet's p90 hour -- i.e. a full allowance buys what a busy
    hour really bought (dino 2026-09-25: $0.91 / $19.87, ~22 median items). Always capped at
    allowance_pct / MIN_ITEMS_PER_HOUR, which is also the answer when there is no history at
    all: a cold start must still move several items, never pack one item as the whole hour.

    Returns one median-complexity entry (fanout.calibrate() reads it back as exactly that
    unit), tagged "source": "cold_start" so gru's report can say which path priced the hour.
    """
    if allowance_pct <= 0:
        return []
    complexity_by_item = complexity_by_item or {}
    usable = [r for r in history_runs
              if isinstance(r.get("cost_usd"), (int, float)) and r["cost_usd"] > 0]
    per_item = sorted(u for r in usable for u in _unit_usd_per_item(r, complexity_by_item))
    hours: dict[int, float] = {}
    for r in usable:
        if isinstance(r.get("recorded_at"), (int, float)):
            h = int(r["recorded_at"] // 3600)
            hours[h] = hours.get(h, 0.0) + r["cost_usd"]
    cap = allowance_pct / MIN_ITEMS_PER_HOUR
    unit = cap
    if per_item and hours:
        busy = sorted(hours.values())
        busy_hour_usd = busy[min(len(busy) - 1, int(BUSY_HOUR_PERCENTILE * len(busy)))]
        median_item_usd = per_item[len(per_item) // 2]
        unit = min(cap, allowance_pct * median_item_usd / busy_hour_usd)
    return [{"pct": unit, "complexity": fanout.DEFAULT_COMPLEXITY, "source": "cold_start"}]


def observe(window_runs: list[dict], history_runs: list[dict], allowance_pct: float,
            complexity_by_item: dict[str, int] | None = None) -> list[dict]:
    """The --observed gru packs against: to_observed() over the recent window when it holds at
    least WARM_MIN_RUNS real runs, else cold_start_observed(). Either way the implied median-item
    unit never exceeds allowance_pct / MIN_ITEMS_PER_HOUR (entries are scaled down to it)."""
    usable = [r for r in window_runs
              if isinstance(r.get("cost_usd"), (int, float)) and r["cost_usd"] > 0]
    if len(usable) < WARM_MIN_RUNS:
        return cold_start_observed(history_runs, allowance_pct, complexity_by_item)
    observed = to_observed(usable, allowance_pct, complexity_by_item)
    unit = fanout.calibrate(observed)
    cap = allowance_pct / MIN_ITEMS_PER_HOUR
    if unit and unit > cap:
        observed = [{**o, "pct": o["pct"] * cap / unit} for o in observed]
    return observed


def minion_cost_history(conn, member: str = "minion", hours: float = HISTORY_HOURS) -> list[dict]:
    """One (item_id, cost_usd, recorded_at) per real run over `hours` -- DISTINCT on run_id, so
    the started/terminal row pair every run leaves counts once."""
    since = time.time() - hours * 3600
    cur = conn.execute(
        "SELECT MAX(item_id), MAX(cost_usd), MIN(recorded_at) FROM runs "
        "WHERE member = ? AND recorded_at >= ? AND cost_usd IS NOT NULL GROUP BY run_id",
        (member, since),
    )
    return [{"item_id": i, "cost_usd": c, "recorded_at": t} for i, c, t in cur.fetchall()]


def recent_minion_costs(conn, member: str = "minion", hours: float = 2.0) -> list[dict]:
    """Real (item_id, cost_usd) pairs from fleet.db -- the same member/window gru.md step 3b's
    own calibration query already reads (`recorded_at > now - 2 hours`, member='minion')."""
    since = time.time() - hours * 3600
    cur = conn.execute(
        "SELECT item_id, cost_usd FROM runs "
        "WHERE member = ? AND recorded_at >= ? AND cost_usd IS NOT NULL",
        (member, since),
    )
    return [{"item_id": item_id, "cost_usd": cost_usd} for item_id, cost_usd in cur.fetchall()]


def recent_minion_batch_turns(conn, member: str = "minion", hours: float = 48.0) -> list[dict]:
    """Real (item_id, num_turns) pairs from fleet.db, DISTINCT on run_id -- fanout.py's
    `pack_batches`/`calibrate_batch_turns` need whole-PASS turn totals, one row per real batch
    RUN, not per underlying issue -- a batched minion's item_id is underscore-joined
    ("64_99_143", from run_member.sh's --items) and its num_turns already covers the whole
    batch pass, so no further grouping is needed here beyond de-duping the started/terminal
    row pair every real run leaves (same (run_id, recorded_at) composite-key shape
    claim_history.py's minion_runs_for_item already de-dupes for the identical reason).

    A longer default window than recent_minion_costs (48h vs 2h): batching is new as of
    2026-09-14, so real batch-pass history accumulates slowly at first -- a 2h window would
    read empty for days. Widen back toward 2h once enough real batch passes exist that a
    shorter window reliably has usable rows; that crossover is a judgment call for whoever
    reads a thin `[]` result and notices the window is still too tight.
    """
    since = time.time() - hours * 3600
    cur = conn.execute(
        "SELECT run_id, item_id, MAX(num_turns) FROM runs "
        "WHERE member = ? AND recorded_at >= ? AND num_turns IS NOT NULL "
        "GROUP BY run_id",
        (member, since),
    )
    return [{"run_id": run_id, "item_id": item_id, "num_turns": num_turns}
            for run_id, item_id, num_turns in cur.fetchall()]


def to_batch_observed(runs: list[dict],
                      complexity_by_item: dict[str, int] | None = None) -> list[dict]:
    """[{"run_id":.., "item_id":"64_99_143", "num_turns":..}, ...] -> fanout.py
    `pack_batches`'s --observed shape: [{"turns": float, "items": [{"complexity":c}, ...]}, ...].

    `item_id` is underscore-split into its individual issue numbers (run_member.sh's --items
    join, see its own comment for why); each is looked up in `complexity_by_item` (same
    median-default convention as to_observed above -- an item missing from the map is treated
    as fanout.DEFAULT_COMPLEXITY, never free, so it still counts toward the batch's weight).

    A single-item run's item_id (no underscore) still works unchanged -- split("_") on a
    string with no underscore returns a one-element list, so a legacy `--item <n>` (never
    `--items`) run calibrates exactly like a batch of size 1.

    Returns [] when there is nothing usable -- same refuse-to-invent discipline as
    to_observed(): the caller (fanout.py's calibrate_batch_turns, fed this as --observed)
    already errors loudly on an empty/unusable --observed.
    """
    complexity_by_item = complexity_by_item or {}
    usable = [r for r in runs
             if isinstance(r.get("num_turns"), (int, float)) and r["num_turns"] > 0
             and r.get("item_id")]
    return [
        {
            "turns": r["num_turns"],
            "items": [
                {"complexity": complexity_by_item.get(n, fanout.DEFAULT_COMPLEXITY)}
                for n in str(r["item_id"]).split("_") if n
            ],
        }
        for r in usable
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert real fleet.db cost_usd into fanout.py's --observed %-of-week shape, "
                     "or real batch-pass num_turns into fanout.py batches' --observed shape.")
    ap.add_argument("--allowance-pct", type=float,
                    help="this pass's per_diem_hourly_pct-derived allowance (already buffered, "
                         "same value passed to fanout.py --allowance-pct); required unless --batch-turns")
    ap.add_argument("--batch-turns", action="store_true",
                    help="emit fanout.py batches' --observed shape (real minion batch-pass "
                         "num_turns) instead of the default %%-of-week cost shape")
    ap.add_argument("--member", default="minion",
                    help="whose runs to calibrate from (default: minion, the real builders)")
    ap.add_argument("--hours", type=float,
                    help="lookback window in hours (default: 2 for cost, 48 for --batch-turns)")
    ap.add_argument("--complexity",
                    help='JSON map of item_id -> fleet:complexity-<n>, e.g. \'{"3253":3}\'; '
                         'an item missing from this map is treated as median, never free')
    ap.add_argument("--db-path", help="override fleet.db path (default: fleet_db.DB_FILE)")
    a = ap.parse_args(argv)

    conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
    fleet_db.sync(conn)
    complexity_by_item = json.loads(a.complexity) if a.complexity else {}

    if a.batch_turns:
        runs = recent_minion_batch_turns(conn, member=a.member, hours=a.hours or 48.0)
        print(json.dumps(to_batch_observed(runs, complexity_by_item)))
        return 0

    if a.allowance_pct is None:
        print("ERROR: --allowance-pct is required unless --batch-turns is set", file=sys.stderr)
        return 2
    runs = recent_minion_costs(conn, member=a.member, hours=a.hours or 2.0)
    history = minion_cost_history(conn, member=a.member)
    observed = observe(runs, history, a.allowance_pct, complexity_by_item)
    if observed and observed[0].get("source") == "cold_start":
        print(f"cost_bridge: cold start ({len(runs)} run(s) in the last {a.hours or 2.0:g}h < "
              f"{WARM_MIN_RUNS}); unit from {len(history)} minion runs over "
              f"{HISTORY_HOURS // 24} days, capped at allowance/{MIN_ITEMS_PER_HOUR}",
              file=sys.stderr)
    print(json.dumps(observed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
