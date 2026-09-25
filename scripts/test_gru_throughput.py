"""gru packed ONE item with 9 fleet:reif-priority items eligible (dino, 2026-09-25 12:48 UTC pass).
Three defects, one test file:

  1. cost_bridge cold start: one run in the 2h window became the whole allowance as ONE item's
     cost (OBS=[{"pct": 36.0}]), so fanout.py packed n=1 regardless of headroom.
  2. an uncalibrated maxx meter (calibrating_unbilled) printed a 36.0000 %-of-week allowance.
  3. quality_gate.py / vision_link_gate.py only took --items as inline argv JSON, which overflows
     with full issue bodies.

Run: python3 scripts/test_gru_throughput.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cost_bridge  # noqa: E402
import fanout  # noqa: E402
import gru_allowance  # noqa: E402
import items_arg  # noqa: E402

# The 12:48 UTC pass's own candidates, in marie's order, with their complexity labels.
PASS_ITEMS = [{"number": n, "complexity": c} for n, c in
              [(7947, 4), (7942, 4), (7937, 5), (7938, 5), (7939, 5), (7940, 5), (7941, 5),
               (7948, 5), (7950, 5)]]


def _history(n_runs=200, cost=1.0, runs_per_hour=10):
    """n_runs single-item runs at $cost, runs_per_hour per hour -> busy hour = $cost*runs_per_hour."""
    now = time.time()
    return [{"item_id": str(i), "cost_usd": cost,
             "recorded_at": now - 3600 * (1 + i // runs_per_hour)} for i in range(n_runs)]


class ColdStartTest(unittest.TestCase):
    def test_one_recent_run_is_not_the_whole_allowance(self):
        # The live shape: one run in the window, allowance 36.0 -> fanout packed n=1 of 9.
        obs = cost_bridge.observe([{"item_id": "7900", "cost_usd": 2.75}], [], 36.0)
        unit = fanout.calibrate(obs)
        self.assertLessEqual(unit, 36.0 / cost_bridge.MIN_ITEMS_PER_HOUR)
        self.assertGreaterEqual(fanout.pack(PASS_ITEMS, 36.0, unit)["n"],
                                cost_bridge.MIN_ITEMS_PER_HOUR)

    def test_cold_start_prices_an_item_from_30_days_of_history(self):
        # median item $1, busy hour $10 -> a full allowance buys ~10 median items.
        obs = cost_bridge.cold_start_observed(_history(), 0.2)
        self.assertEqual(obs[0]["source"], "cold_start")
        self.assertAlmostEqual(fanout.calibrate(obs), 0.2 * 1.0 / 10.0)

    def test_batched_history_runs_split_per_item(self):
        # A $3 run that built 3 median items is three $1 items, not one $3 item.
        hist = [{"item_id": "1_2_3", "cost_usd": 3.0, "recorded_at": time.time() - 3600 * (1 + i)}
                for i in range(20)]
        obs = cost_bridge.cold_start_observed(hist, 1.0)
        self.assertAlmostEqual(fanout.calibrate(obs), 0.25)  # 1/3 capped at allowance/4

    def test_cold_start_is_capped_even_when_history_says_items_are_huge(self):
        hist = [{"item_id": str(i), "cost_usd": 50.0, "recorded_at": time.time() - 3600 * (1 + i)}
                for i in range(10)]
        obs = cost_bridge.cold_start_observed(hist, 0.2)
        self.assertAlmostEqual(fanout.calibrate(obs), 0.2 / cost_bridge.MIN_ITEMS_PER_HOUR)

    def test_no_history_still_yields_several_items(self):
        obs = cost_bridge.cold_start_observed([], 0.2)
        self.assertAlmostEqual(fanout.calibrate(obs), 0.2 / cost_bridge.MIN_ITEMS_PER_HOUR)

    def test_zero_allowance_still_refuses_to_invent(self):
        self.assertEqual(cost_bridge.observe([], _history(), 0.0), [])

    def test_warm_window_keeps_the_proportional_derivation(self):
        window = [{"item_id": str(i), "cost_usd": 1.0} for i in range(10)]
        obs = cost_bridge.observe(window, _history(), 0.2)
        self.assertEqual(obs, cost_bridge.to_observed(window, 0.2))
        self.assertAlmostEqual(fanout.calibrate(obs), 0.02)

    def test_warm_window_is_capped_too(self):
        # 5 runs where one is a huge complexity-1 outlier: unit would be > allowance/4.
        window = [{"item_id": "1", "cost_usd": 100.0}] + \
                 [{"item_id": str(i), "cost_usd": 0.01} for i in range(2, 6)]
        obs = cost_bridge.observe(window, [], 0.2, {"1": 1})
        self.assertAlmostEqual(fanout.calibrate(obs), 0.2 / cost_bridge.MIN_ITEMS_PER_HOUR)

    def test_history_query_dedupes_started_and_terminal_rows(self):
        import fleet_db
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            now = time.time()
            recs = [
                {"run_id": "r1", "member": "minion", "item_id": "10", "_recorded_at": now - 60,
                 "status": "started"},
                {"run_id": "r1", "member": "minion", "item_id": "10", "_recorded_at": now - 30,
                 "tokens": {"cost_usd": 2.0}},
                {"run_id": "r2", "member": "minion", "item_id": "20_21", "_recorded_at": now - 9 * 86400,
                 "tokens": {"cost_usd": 1.0}},
                {"run_id": "r3", "member": "minion", "item_id": "30", "_recorded_at": now - 40 * 86400,
                 "tokens": {"cost_usd": 9.0}},
                {"run_id": "r4", "member": "gru", "item_id": "", "_recorded_at": now - 60,
                 "tokens": {"cost_usd": 5.0}},
            ]
            (d / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
            conn = fleet_db.connect(d / "fleet.db")
            fleet_db.sync(conn, runs_file=d / "runs.jsonl")
            hist = cost_bridge.minion_cost_history(conn)
        self.assertEqual(sorted((h["item_id"], h["cost_usd"]) for h in hist),
                         [("10", 2.0), ("20_21", 1.0)])


class UncalibratedMeterTest(unittest.TestCase):
    def test_calibrating_unbilled_is_not_36_percent_of_a_week(self):
        out = gru_allowance.compute("60.0000", "0.6", "calibrating_unbilled", "0.6")
        self.assertEqual(out, f"{100 / 168 * 0.6 * 0.6:.4f}")  # 0.2143

    def test_operator_override(self):
        self.assertEqual(gru_allowance.compute("60", "0.6", "calibrating_unbilled", "0.6", "0.5"),
                         "0.5000")

    def test_real_readings_unchanged(self):
        self.assertEqual(gru_allowance.compute("0.0142", "0.75", "ok", "0.2"), "0.0106")
        self.assertEqual(gru_allowance.compute("0.0000", "0.75", "over"), "0.0000")
        self.assertEqual(gru_allowance.compute(None, "0.75", None), "")

    def test_cli_reads_the_label_from_env(self):
        env = {**os.environ, "FLEET_SHARE_CEILING_PCT": "60.0000", "FLEET_GRU_ALLOWANCE_FRACTION": "0.6",
               "FLEET_SHARE_FRACTION": "0.6", "FLEET_MAXX_LABEL": "calibrating_unbilled"}
        env.pop("FLEET_UNCALIBRATED_ALLOWANCE_PCT", None)
        out = subprocess.run([sys.executable, str(HERE / "gru_allowance.py")], env=env,
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "0.2143")
        self.assertIn("uncalibrated", out.stderr)


class PassReplayTest(unittest.TestCase):
    def test_the_12_48_pass_now_packs_every_eligible_item_into_more_than_one_batch(self):
        """Before: allowance 36.0, OBS=[{"pct": 36.0}] -> n=1, one minion. After, same
        inputs (calibrating_unbilled meter, one recent run, dino-like 30-day history)."""
        allowance = float(gru_allowance.compute("60.0000", "0.6", "calibrating_unbilled", "0.6"))
        hist = _history(n_runs=400, cost=0.91, runs_per_hour=22)  # busy hour ~$20, as on dino
        obs = cost_bridge.observe([{"item_id": "7900", "cost_usd": 2.75}], hist, allowance)
        packed = fanout.pack(PASS_ITEMS, allowance, fanout.calibrate(obs))
        self.assertEqual(packed["n"], len(PASS_ITEMS))
        self.assertFalse(packed["over_allowance"])
        batches = fanout.pack_batches(packed["chosen"], 0, 10.0, target_items=8)
        self.assertGreater(batches["n_batches"], 1)


class ItemsArgTest(unittest.TestCase):
    ITEMS = [{"number": 1, "labels": [{"name": "quality:solid"}],
              "body": "Vision-link: none (maintenance)\n" + "x" * 200_000,
              "comments": [{"body": "Given a signed-out visitor, when they POST /claim, then the API returns 401."}]}]

    def test_load_items_three_ways(self):
        self.assertEqual(items_arg.load_items('[{"number": 1}]'), [{"number": 1}])
        self.assertEqual(items_arg.load_items("-", io.StringIO('[{"number": 2}]')), [{"number": 2}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"number": 3}], f)
        try:
            self.assertEqual(items_arg.load_items(f.name), [{"number": 3}])
        finally:
            os.unlink(f.name)

    def _gate(self, script, *args, stdin=None):
        out = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin,
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_gates_take_full_bodies_from_a_file_and_stdin(self):
        # 3 x 200KB = past the 128KB single-argument limit that inline argv hits.
        items = [dict(self.ITEMS[0], number=n) for n in (1, 2, 3)]
        payload = json.dumps(items)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(payload)
        try:
            for script in ("quality_gate.py", "vision_link_gate.py"):
                self.assertEqual(self._gate(script, "--items", f.name)["eligible"], [1, 2, 3], script)
                self.assertEqual(self._gate(script, "--items", "-", stdin=payload)["eligible"],
                                 [1, 2, 3], script)
        finally:
            os.unlink(f.name)

    def test_inline_json_still_works(self):
        small = json.dumps([dict(self.ITEMS[0], body="Vision-link: none (maintenance)")])
        self.assertEqual(self._gate("quality_gate.py", "--items", small)["eligible"], [1])
        self.assertEqual(self._gate("vision_link_gate.py", "--items", small)["eligible"], [1])


if __name__ == "__main__":
    unittest.main()
