"""A direction Reif already gave, said again on a later day, counts as a repeat (fleet miss)."""
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_metrics  # noqa: E402
import intent_repeats as ir  # noqa: E402

INTENT = """# INTENT.md

## Keys
- 2026-10-16 hospitals: hospital | health system
- 2026-10-17 never-twice: explain this daily

## Standing decisions
- 2026-10-16 "I thought we didn't do hospitals" -> hospitals out of Jobs
"""


def at(stamp: str) -> float:
    return dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=ir.CENTRAL).timestamp()


class IntentRepeats(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.desk = self.tmp / "desk"
        self.desk.mkdir()
        self.intent = self.tmp / "INTENT.md"
        self.intent.write_text(INTENT)

    def item(self, stamp: str, text: str):
        (self.desk / f"{stamp}.md").write_text(text)

    def test_keys_come_from_the_keys_section_only(self):
        keys = ir.parse_keys(INTENT)
        self.assertEqual([k[1] for k in keys], ["hospitals", "never-twice"])
        self.assertEqual(keys[0][2], ["hospital", "health system"])

    def test_saying_it_again_on_a_later_day_is_a_repeat(self):
        self.item("20261016-231649", "I thought we didn't do HOSPITALS")  # the day he said it
        self.item("20261017-090000", "hospitals are still on /jobs")       # the next day
        self.item("20261017-100000", "make the Give card hold Contact")    # something new
        n = ir.count_repeats(at("20261017-120000"), 48, self.intent, self.desk)
        self.assertEqual(n, 1)

    def test_a_relay_counts_only_reifs_quoted_words(self):
        # fk#1740/#1796: G's brief citing a rule back is the fleet obeying it
        self.item("20261017-090000", "baby-g: the public /jobs number (hospitals out) is live")
        self.item("20261017-091000", "From baby-g: Reif asks 'why are hospitals still on /jobs?'")
        self.item("20261017-092000", "[desk 9:20] health system rows filtered, per the rule")
        self.item("20261017-093000", "Make the hospital rows go away")  # Reif's own words
        self.item("20261017-094000", 'REIF (Oct 17), hospital page. His words: "ship the Give card"')
        n = ir.count_repeats(at("20261017-120000"), 24, self.intent, self.desk)
        self.assertEqual(n, 2)

    def test_no_keys_is_unmeasured_not_zero(self):
        self.intent.write_text("# INTENT.md\n\n## Standing decisions\n")
        self.assertIsNone(ir.count_repeats(at("20261017-120000"), 24, self.intent, self.desk))

    def test_metric_is_per_day(self):
        self.item("20261017-090000", "hospitals again")
        self.item("20261017-091000", "the health system rows")
        os.environ["FLEET_LOG_DIR"], os.environ["FLEET_DESK_INBOX"] = str(self.tmp), str(self.desk)
        v = fleet_metrics.compute("reif_repeats_per_day", [], at("20261017-120000"), 48)
        self.assertEqual(v, 1.0)
        self.assertEqual(fleet_metrics.direction("reif_repeats_per_day"), "lower")

    def test_pregate_fires_on_a_desk_item_newer_than_intent(self):
        os.utime(self.intent, (at("20261017-080000"),) * 2)
        self.assertTrue(ir.pregate(at("20261017-120000"), self.intent, self.desk).startswith("green"))
        self.item("20261017-090000", "a new direction")
        self.assertTrue(ir.pregate(at("20261017-120000"), self.intent, self.desk).startswith("intent: 1"))
        self.assertEqual(ir.pregate(at("20261018-001500"), self.intent, self.desk), "daily")


if __name__ == "__main__":
    unittest.main()
