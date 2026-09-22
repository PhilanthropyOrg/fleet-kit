"""A never-anchored account reads verdict=calibrating with lifetime_billed=0 forever -- maxx
has no data for it, not bad data. get_headroom() must treat that as full headroom (1.0), not
as an unreadable meter (None): a broken/unreadable meter falls back to whatever cached
allowance the caller last had, and a fresh account has none to fall back to.

Confirmed live 2026-09-22: philanthropy (freshly onboarded to the fleet, statusline hook wired
same day, never billed a token through maxx) read verdict=calibrating/lifetime_billed=0
indefinitely. Before this fix that fell through to `maxx_verdict_calibrating` -> None ->
account_pool.sh's _account_pool_order sorted it into the `unknown` bucket, which sorts LAST --
behind gmail (week_bank_pct=-5.7, already over its share) and tgp (week_bank_pct=-12.8). The
fleet kept draining two struggling accounts while a fresh one with its full week sat unused,
and sentry (the deploy-verification member) got PACED to zero against gmail's negative bank
while philanthropy had headroom the whole time.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maxx_reader  # noqa: E402


def _fetcher(payload):
    return lambda *a, **k: payload


class CalibratingUnbilledTest(unittest.TestCase):
    def test_calibrating_with_zero_lifetime_billed_reads_as_full_headroom(self):
        frac, label, allowance = maxx_reader.get_headroom(
            base_url="https://x", handle="philanthropy", key="k",
            fetcher=_fetcher({"verdict": "calibrating", "lifetime_billed": 0}),
        )
        self.assertEqual(frac, 1.0)
        self.assertEqual(label, "calibrating_unbilled")

    def test_calibrating_with_real_billed_spend_still_reads_unreadable(self):
        """An account that HAS billed but still reads calibrating is a genuinely broken or
        mid-recalculation meter -- real spend exists that this reading fails to reflect, so it
        must NOT be treated as full headroom. Only lifetime_billed==0 is the safe case."""
        frac, label, allowance = maxx_reader.get_headroom(
            base_url="https://x", handle="tgp", key="k",
            fetcher=_fetcher({"verdict": "calibrating", "lifetime_billed": 500000}),
        )
        self.assertIsNone(frac)
        self.assertEqual(label, "maxx_verdict_calibrating")

    def test_calibrating_with_missing_lifetime_billed_field_stays_unreadable(self):
        """Missing the field entirely (not explicitly 0) must not be treated as the safe
        case -- an absent field is a different failure mode than a confirmed-zero one."""
        frac, label, allowance = maxx_reader.get_headroom(
            base_url="https://x", handle="gmail", key="k",
            fetcher=_fetcher({"verdict": "calibrating"}),
        )
        self.assertIsNone(frac)

    def test_a_real_ok_verdict_is_unaffected(self):
        """Mutation guard: the new branch must not swallow the ordinary healthy path."""
        frac, label, allowance = maxx_reader.get_headroom(
            base_url="https://x", handle="tgp", key="k",
            fetcher=_fetcher({"verdict": "ok", "week_bank_pct": 42.0, "lifetime_billed": 900}),
        )
        self.assertEqual(frac, 0.42)
        self.assertEqual(label, "ok")


if __name__ == "__main__":
    unittest.main()
