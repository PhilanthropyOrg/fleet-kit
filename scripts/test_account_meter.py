"""One unreadable meter must not stop gru building, and the hour cap follows the week's pace
(2026-10-16: maxx 502 + tgp unreadable -> "built nothing"; phil 87% left at 84% elapsed)."""
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

import account_meter  # noqa: E402
import build_lane_rule  # noqa: E402

TGP = {"name": "tgp", "error": "no reading: long-lived setup token"}
PHIL = {"name": "phil", "week_left": 87, "elapsed": 84, "walled": False}
REIF = {"name": "reif", "week_left": 51, "elapsed": 52, "walled": False}


def meter_file(rows, ts=None):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"ts": time.time() if ts is None else ts, "rows": rows}, f)
    f.close()
    return f.name


def allowance(rows):
    env = {k: v for k, v in os.environ.items()
           if k not in ("FLEET_SHARE_CEILING_PCT", "FLEET_MAXX_LABEL", "FLEET_UNCALIBRATED_ALLOWANCE_PCT")}
    env.update(FLEET_METER_FILE=meter_file(rows), FLEET_SHARE_FRACTION="0.6",
               FLEET_GRU_ALLOWANCE_FRACTION="0.6")
    r = subprocess.run([sys.executable, str(HERE / "gru_allowance.py")], env=env,
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


class Allowance(unittest.TestCase):
    def test_unreadable_row_still_allows_builds_on_a_readable_account(self):
        # maxx gave no ceiling; tgp cannot be read; phil can and has room.
        self.assertEqual(allowance([TGP, PHIL]), "1.0312")  # (87-10)/(16% of 168h) * .6 * .6

    def test_unreadable_unwalled_account_alone_is_even_pace_not_blind(self):
        self.assertEqual(allowance([TGP]), "0.2143")

    def test_every_account_walled_prints_nothing(self):
        self.assertEqual(allowance([{**TGP, "walled": True}, {**PHIL, "walled": True}]), "")

    def test_stale_file_is_no_reading(self):
        self.assertEqual(account_meter.load(meter_file([PHIL], ts=time.time() - 4 * 3600)), [])


class HourCap(unittest.TestCase):
    def test_behind_pace_raises_the_cap(self):
        self.assertEqual(build_lane_rule.hour_cap({"FLEET_MINION_MAX_PER_HOUR": "1"}, [TGP, PHIL, REIF]), 5)

    def test_on_pace_keeps_the_dial(self):
        self.assertEqual(build_lane_rule.hour_cap({"FLEET_MINION_MAX_PER_HOUR": "1"}, [REIF]), 1)

    def test_no_meter_keeps_the_dial_and_no_cap_stays_no_cap(self):
        self.assertEqual(build_lane_rule.hour_cap({"FLEET_MINION_MAX_PER_HOUR": "1"}, []), 1)
        self.assertEqual(build_lane_rule.hour_cap({}, [PHIL]), 0)

    def test_cap_never_passes_the_ceiling(self):
        rows = [{"name": "x", "week_left": 99, "elapsed": 99}]
        self.assertEqual(build_lane_rule.hour_cap({"FLEET_MINION_MAX_PER_HOUR": "1"}, rows),
                         account_meter.MAX_PACED_PER_HOUR)


if __name__ == "__main__":
    unittest.main()
