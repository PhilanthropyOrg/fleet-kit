"""Nothing waits on a human (Reif, 2026-09-28): "zero should ever wait on a human" and, on the
asks, "if it's an opinion, make the opinion yourself."

THE GAP: 9 fleet:reif-priority issues sat on fleet:needs-human-op and 4 on fleet:dead-end-blocked,
and 15 asks sat open, most of them opinions (decision/acceptance/idea/infra) the fleet could make.
Now: `ask.py file` with an opinion class files a notice and the member proceeds; only the
one-way-door classes (credential, money) open a question. The member specs say the same, and
marie un-parks what is parked on something that is not a one-way door.

Run: python3 scripts/test_zero_human_waits.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import ask  # noqa: E402
import fleet_db  # noqa: E402


def _file(d, cls):
    rc = ask.main(["--db-path", str(Path(d) / "fleet.db"),
                   "--authority-path", str(Path(d) / "none" / "authority.json"),
                   "file", "--member", "marie", "--why", f"a {cls} question", "--class", cls,
                   "--no-notify"])
    assert rc == 0
    conn = fleet_db.connect(Path(d) / "fleet.db")
    return ask.list_asks(conn, status="all")[0]["status"]


class OpinionsAreDecidedNotAsked(unittest.TestCase):
    def test_opinion_classes_file_a_notice_with_no_grant(self):
        for cls in ("decision", "acceptance", "idea", "infra", "product-copy", "pricing",
                    "external-merge"):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(_file(d, cls), "notice", cls)

    def test_one_way_doors_still_ask(self):
        for cls in ("credential", "money"):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(_file(d, cls), "open", cls)


class SpecsSayNothingWaitsOnAHuman(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text()

    def test_persona_law_has_the_rule(self):
        law = self.read("agents/persona_law.md")
        self.assertIn("## 2b. Nothing waits on a human", law)
        self.assertIn("--class credential|money", law)

    def test_minion_blocked_line_is_only_a_one_way_door(self):
        minion = self.read("members/minion/minion.md")
        self.assertNotIn("an open question only Reif can answer", minion)
        self.assertIn("one-way door", minion)

    def test_marie_unparks_and_decides_scope(self):
        marie = self.read("members/marie/marie.md")
        self.assertIn("## Part A0 — un-park", marie)
        self.assertNotIn("always waits for Reif himself", marie)

    def test_gru_honors_unparked_and_answers_every_non_door_ask(self):
        gru = self.read("members/gru/gru.md")
        self.assertIn("`Un-parked:` comment", gru)
        self.assertIn("every class except `credential` and `money`", gru)


if __name__ == "__main__":
    unittest.main()
