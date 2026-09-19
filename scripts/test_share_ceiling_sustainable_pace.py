"""fk#1169: the share ceiling is the sustainable pace, not pace minus the per-diem allowance.

`per_diem_hourly_pct` is the maxx server's hourly ALLOWANCE (per-diem / 24), not this hour's
consumption. Subtracting it left the fleet the sliver between two nearly-equal numbers. Live
numbers for the tgp account, 2026-09-19 13:48Z: sustainable 0.55, per_diem_hourly 0.464,
reserved 0, FLEET_SHARE_FRACTION 0.6 -> the fleet got 0.052 %/h and read PACED; the meter
itself said on_pace with 71% of the week left. Reif: "just sustainable pace".
"""
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import maxx_share_ceiling  # noqa: E402

TGP_LIVE = {"verdict": "ok", "sustainable_pct_per_hour": 0.55, "per_diem_hourly_pct": 0.464,
            "reserved_pct": 0, "session_used_pct": 14, "five_reset_in_sec": 7737}


def ceiling(budget, share="0.6"):
    orig = maxx_share_ceiling.get_headroom
    maxx_share_ceiling.get_headroom = lambda: (1.0, "ok", budget)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert maxx_share_ceiling.main(["prog", share]) == 0
        return float(buf.getvalue().strip())
    finally:
        maxx_share_ceiling.get_headroom = orig


class SustainablePace(unittest.TestCase):
    def test_the_live_tgp_reading_yields_the_share_of_sustainable_pace(self):
        # 0.55 * 0.6 = 0.33, not (0.55 - 0.464) = 0.086
        self.assertAlmostEqual(ceiling(TGP_LIVE), 0.33, places=4)

    def test_others_reservations_still_come_off(self):
        self.assertAlmostEqual(ceiling({**TGP_LIVE, "reserved_pct": 0.30}), 0.25, places=4)  # min(0.33, 0.55-0.30)

    def test_allowance_field_no_longer_moves_the_ceiling(self):
        self.assertEqual(ceiling({**TGP_LIVE, "per_diem_hourly_pct": 0.90}), ceiling(TGP_LIVE))

    def test_block_clamp_and_over_verdict_still_hold(self):
        self.assertEqual(ceiling({**TGP_LIVE, "session_used_pct": 77, "five_reset_in_sec": 12534}), 0.0)


if __name__ == "__main__":
    unittest.main()
