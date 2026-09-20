"""Reif 2026-09-19: "keep going with standardization -- so we don't revert back into oblivion."

CI runs an explicit list of test files. A new scripts/test_*.py is therefore green forever
unless someone remembers to add a step -- which is how 33 of 62 files ended up unrun (fk#1156),
hiding a real console bug (an /api/minion_runs fetch that skipped the path prefix) for as long
as nobody looked. This test fails when a test file exists that no CI step invokes.

To exempt a file deliberately, add it to KNOWN_UNRUN with the reason. An empty reason is not a
reason: the point is that skipping is a decision someone wrote down, not an oversight."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"

# file -> why it is deliberately not in CI. Keep this SHORT; a growing list is the smell.
KNOWN_UNRUN: dict[str, str] = {
    "test_console_path_prefix.py":
        "fk#1183: hand-rolled API stubs drifted from the page; needs porting to console_api_stubs",
    "test_fleet_home_base_path.py": "fk#1183: same stub drift",
    "test_fleet_home_health.py": "fk#1183: same stub drift",
    "test_queue_filter_wiring.py": "fk#1183: same stub drift",
    "test_fleet_view_subprocess_env.py":
        "fk#1184: REAL regression, not stub drift -- fleet_view_server spawns board_github.py "
        "with no env=, so it inherits a bare environment and falls back to the host default",
    "test_pretest_push_hook.py":
        "fk#1183: asserts the pre-push hook leaves non-push commands alone; the hook now blocks "
        "a whole-tree pytest anywhere (fk#1166), so the test encodes the pre-1166 contract",
}


class CiRunsEveryTestFile(unittest.TestCase):
    def test_every_test_file_is_either_run_or_deliberately_exempt(self):
        ci = CI.read_text()
        files = sorted(p.name for p in (ROOT / "scripts").glob("test_*.py"))
        unrun = [f for f in files if f not in ci and f not in KNOWN_UNRUN]
        self.assertEqual(unrun, [], "these test files exist but CI never runs them -- add a step "
                                    "to .github/workflows/ci.yml, or an entry to KNOWN_UNRUN "
                                    "with the reason: " + repr(unrun))

    def test_exemptions_carry_a_real_reason(self):
        for name, why in KNOWN_UNRUN.items():
            self.assertTrue(len(why.strip()) > 20, f"{name}: exemption needs a real reason, got {why!r}")

    def test_exemptions_are_not_stale(self):
        # an exemption for a file that no longer exists, or that CI now runs, is noise
        ci = CI.read_text()
        for name in KNOWN_UNRUN:
            self.assertTrue((ROOT / "scripts" / name).exists(), f"{name}: exempted but the file is gone")
            self.assertNotIn(name, ci, f"{name}: CI runs it now -- drop the exemption")


if __name__ == "__main__":
    unittest.main()
