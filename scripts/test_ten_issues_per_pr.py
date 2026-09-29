#!/usr/bin/env python3
"""At least 10 issues per minion PR (Reif, 2026-09-29).

"the bottleneck beforehand was human brains for fixing bugs, hence smaller prs -- but that is not
the bottleneck now as lowest models can fix bugs, meaning we should pack more issues into prs"
... "make the minimum 10 issues". Every PR costs a full CI run per push; philanthropy hit its 50k
GitHub Actions minutes on 09-29 with minion PRs averaging 1.3 issues (a c>=5 item always went
solo, batches capped at 3).

RED without the change: the CLI packs 12 median items as 12 solo minions. GREEN: one batch of 10,
the 2 left over wait for the next pass -- unless no batch reaches 10 (a thin backlog still ships).
Run: python3 scripts/test_ten_issues_per_pr.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fanout  # noqa: E402


def _cli(items, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_MINION_")}
    e.update(env)
    r = subprocess.run([sys.executable, str(HERE / "fanout.py"), "batches", "--turn-budget", "0",
                        "--unit-turns", "30", "--items", json.dumps(items)],
                       capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _sizes(r):
    return [len(b["items"]) for b in r["batches"]]


class TenPerPr(unittest.TestCase):
    def test_median_items_pack_ten_to_a_batch_and_leftovers_wait(self):
        items = [{"number": 9000 + i, "complexity": 5, "area": ""} for i in range(12)]
        r = _cli(items)
        self.assertEqual(_sizes(r), [10])
        self.assertEqual([d["number"] for d in r["deferred"]], [9010, 9011])
        self.assertIn("minimum", r["deferred"][0]["why"])

    def test_a_thin_backlog_still_ships(self):
        items = [{"number": 9100 + i, "complexity": 4, "area": ""} for i in range(6)]
        self.assertEqual(_sizes(_cli(items)), [6], "fewer than 10 ready: ship them, never idle")

    def test_instance_env_still_wins(self):
        items = [{"number": 9200 + i, "complexity": 3, "area": ""} for i in range(4)]
        self.assertEqual(_sizes(_cli(items, FLEET_MINION_TARGET_ITEMS="2", FLEET_MINION_MIN_ITEMS="0")),
                         [2, 2])

    def test_ten_median_items_fit_the_minion_timeout(self):
        spec = json.loads((HERE.parent / "members/minion/minion.fleet.json").read_text())
        timeout = spec["mandate"]["limits"]["timeout_s"]
        self.assertEqual(timeout, spec["timeout_s"])
        r = fanout.pack_batches([{"number": i, "complexity": 5} for i in range(10)], 0, 30,
                                solo_complexity_floor=11, target_items=10, timeout_s=timeout,
                                unit_seconds=600, min_items=10)
        self.assertEqual(_sizes(r), [10], r["max_batch_weight"])


if __name__ == "__main__":
    unittest.main()
