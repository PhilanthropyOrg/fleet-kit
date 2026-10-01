"""A charter is loaded into the prompt on every pass, so its size is paid for every pass.

Reif, 2026-10-01: the rulebooks are "likely full of junk, old rules". gru.md had grown to 67 KB
and nerd.md to 37 KB, most of it dated incident stories layered over weeks. gru ran 533 passes
in 14 days and nerd 325, each one reading the whole file. The stories now live in
docs/charter-history/<member>.md, which no prompt loads.

This test measures two files; it gates no work. It fails when a slimmed charter grows back
past its budget, so the next story goes to the history file instead of into the prompt. If a
charter truly needs more room, raise its number here and say why in the PR.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import member_spec  # noqa: E402

# member -> most bytes its charter file may hold. Set a little above the size it was slimmed
# to on 2026-10-01 (gru 34.0 KB, nerd 20.6 KB), so a small real rule still fits.
BUDGET_BYTES = {"gru": 36_000, "nerd": 22_000}


def over_budget(root: Path = ROOT, budgets: dict = BUDGET_BYTES) -> list[str]:
    """Pure: one line per charter whose file is bigger than its budget."""
    out = []
    for member, budget in budgets.items():
        size = len((root / "members" / member / f"{member}.md").read_bytes())
        if size > budget:
            out.append(f"{member}.md is {size} bytes, budget {budget}")
    return out


class CharterBudget(unittest.TestCase):
    def test_slimmed_charters_stay_inside_their_budget(self):
        self.assertEqual(over_budget(), [], "a charter grew past its budget -- move the story of "
                         "why to docs/charter-history/<member>.md (rules stay, stories go), or "
                         "raise BUDGET_BYTES here and say why in the PR")

    def test_the_history_file_is_never_what_the_runner_loads(self):
        for member in BUDGET_BYTES:
            d = ROOT / "members" / member
            spec = json.loads((d / f"{member}.fleet.json").read_text())
            self.assertEqual(member_spec.behavior_path(spec, ROOT / "members").resolve(),
                             (d / f"{member}.md").resolve())
            # nothing else in the member's folder that a `members/*/*.md` glob would pick up
            self.assertEqual(sorted(p.name for p in d.glob("*.md")), [f"{member}.md"])
            self.assertTrue((ROOT / "docs" / "charter-history" / f"{member}.md").exists(),
                            f"{member}.md cites H§n but its history file is gone")

    def test_every_history_pointer_resolves(self):
        import re
        for member in BUDGET_BYTES:
            charter = (ROOT / "members" / member / f"{member}.md").read_text()
            history = (ROOT / "docs" / "charter-history" / f"{member}.md").read_text()
            for n in sorted(set(re.findall(r"H§(\d+)", charter)), key=int):
                self.assertIn(f"**H§{n}.", history, f"{member}.md cites H§{n}; no such entry")


if __name__ == "__main__":
    unittest.main()
