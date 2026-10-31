"""PHIA-1696: judge-judy runs in two lanes, and the lanes never review the same PR.

Run: python3 scripts/test_judge_judy_lanes.py
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
CLAIM = re.search(r"^claim_pr\(\) \{\n.*?^\}\n", SRC, re.S | re.M).group(0)
LOOP = SRC[SRC.index("PR=${PICK% *}"):SRC.index("DIFF_FILE=$(mktemp")]


def bash(script: str, log_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f"LOG_DIR={log_dir}\n{CLAIM}\n{script}"],
                          capture_output=True, text=True, timeout=30)


class LaneTest(unittest.TestCase):
    def test_a_claimed_pr_is_refused_to_the_other_lane_until_released(self):
        with tempfile.TemporaryDirectory() as d:
            holder = subprocess.Popen(["bash", "-c", f"LOG_DIR={d}\n{CLAIM}\nclaim_pr 7 && echo held && read _"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(bash("claim_pr 7 && echo got || echo busy", d).stdout.strip(), "busy")
            self.assertEqual(bash("claim_pr 8 && echo got || echo busy", d).stdout.strip(), "got")
            holder.communicate("\n")  # the lane exits: its claim goes with it
            self.assertEqual(bash("claim_pr 7 && echo got || echo busy", d).stdout.strip(), "got")

    def test_claiming_the_next_pr_drops_the_last_one(self):
        with tempfile.TemporaryDirectory() as d:
            holder = subprocess.Popen(["bash", "-c", f"LOG_DIR={d}\n{CLAIM}\nclaim_pr 7 && claim_pr 8 && echo held && read _"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(bash("claim_pr 7 && echo got || echo busy", d).stdout.strip(), "got")
            self.assertEqual(bash("claim_pr 8 && echo got || echo busy", d).stdout.strip(), "busy")
            holder.communicate("\n")

    def test_the_loop_claims_then_looks_again_before_reviewing(self):
        self.assertLess(LOOP.index('claim_pr "$PR"'), LOOP.index('RECHECK=$(pick_pr "$PR" "")'))
        self.assertIn('"$RECHECK" != "$PR $HEAD_SHA"', LOOP)
        self.assertEqual(LOOP.count('SKIPPED_THIS_TICK="$SKIPPED_THIS_TICK $PR"'), 2)

    def test_each_lane_has_its_own_tick_lock(self):
        self.assertIn('LOCKFILE="$LOG_DIR/judge-judy${FLEET_LANE:+-lane$FLEET_LANE}.lock"', SRC)

    def test_run_member_passes_the_lane_and_cron_runs_lane_two(self):
        rm = (ROOT / "scripts" / "run_member.sh").read_text()
        self.assertIn('FLEET_LANE="$LANE" "$RUNNER_PATH"', rm)
        ep = (ROOT / "entrypoint.sh").read_text()
        self.assertIn('for lane in $(seq 2 "${FLEET_REVIEW_LANES:-2}"); do', ep)
        self.assertIn("run_member.sh judge-judy --task lane=$lane >>", ep)


if __name__ == "__main__":
    unittest.main()
