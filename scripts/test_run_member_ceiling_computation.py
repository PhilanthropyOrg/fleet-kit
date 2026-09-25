"""run_member.sh's FLEET_SHARE_CEILING_PCT computation must actually print a number (gh#1222).

RED without this fix: the ceiling block's python3 -c one-liner nested single-quoted
float('$HEADROOM_FRACTION') calls INSIDE an f-string's {...} expression delimited by the same
quote character -- a SyntaxError ("f-string: unmatched '('") on every Python version this kit
runs. The error went to the block's own 2>>"$LOG" redirect and was silently swallowed:
CEILING_PCT was empty on every single pass since gh#1215 (the maxx gas-gauge rewrite) landed,
so pacing_gate.py never saw a ceiling at all and every pass behaved as if FLEET_SHARE_FRACTION
were never set -- the exact "member never paces" failure mode gh#1215 was supposed to fix.

Caught verifying gh#1215/#1216/#1220 live on dino: a fresh gru pass logged
`FLEET_SHARE_CEILING_PCT= (headroom_fraction=0.0 x FLEET_SHARE_FRACTION=0.6)` -- headroom_fraction
resolved correctly, FLEET_SHARE_FRACTION resolved correctly, but the multiply produced nothing.

This drives the ACTUAL block extracted from run_member.sh (not a copy) through real bash, the
same harness shape test_run_member_key_follows_handle.py uses for its own block -- so a future
edit that reintroduces string-interpolation-into-Python-source is caught the same way.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _extract_ceiling_block(run_member_text: str) -> str:
    start_marker = 'if [ "$DRY_RUN" -ne 1 ] && [ "${FLEET_SHARE_FRACTION:-1.0}" != "1.0" ]; then'
    start = run_member_text.find(start_marker)
    assert start != -1, "ceiling block not found at its expected shape in run_member.sh"
    end = run_member_text.find("\nfi\n", start)
    assert end != -1, "could not find the ceiling block's closing fi"
    return run_member_text[start:end + len("\nfi")]


class CeilingComputationTest(unittest.TestCase):
    def setUp(self):
        self.run_member_text = (ROOT / "scripts" / "run_member.sh").read_text()
        self.block = _extract_ceiling_block(self.run_member_text)

    def _run_block(self, headroom_fraction_json: str, share: str) -> dict:
        with_stub_reader = f"""#!/bin/bash
set -uo pipefail
DRY_RUN=0
KIT_DIR=/tmp/ceiling-test-stub-$$
mkdir -p "$KIT_DIR/scripts"
cat > "$KIT_DIR/scripts/maxx_reader.py" << 'READER_EOF'
print('{headroom_fraction_json}')
READER_EOF
LOG=/dev/null
MEMBER=test-member
FLEET_SHARE_FRACTION={share}
{self.block}
echo "FLEET_SHARE_CEILING_PCT=$FLEET_SHARE_CEILING_PCT"
echo "FLEET_MAXX_LABEL=$FLEET_MAXX_LABEL"
rm -rf "$KIT_DIR"
"""
        proc = subprocess.run(["bash", "-c", with_stub_reader], capture_output=True, text=True, timeout=10)
        out = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k] = v
        out["_stderr"] = proc.stderr
        return out

    def test_zero_headroom_prints_a_real_zero_not_empty(self):
        result = self._run_block('{"headroom_fraction": 0.0, "label": "ok"}', "0.6")
        self.assertEqual(result.get("FLEET_SHARE_CEILING_PCT"), "0.0000",
                          f"ceiling must print a real 0.0000, not empty: {result}")

    def test_full_headroom_computes_the_correct_slice(self):
        result = self._run_block('{"headroom_fraction": 1.0, "label": "calibrating_unbilled"}', "0.6")
        self.assertEqual(result.get("FLEET_SHARE_CEILING_PCT"), "60.0000")

    def test_label_is_exported_for_gru_allowance(self):
        """gru_allowance.py swaps the 60.0000 ceiling of an uncalibrated meter for a fixed
        allowance -- it can only do that if run_member.sh tells it the label (2026-09-25)."""
        result = self._run_block('{"headroom_fraction": 1.0, "label": "calibrating_unbilled"}', "0.6")
        self.assertEqual(result.get("FLEET_MAXX_LABEL"), "calibrating_unbilled")

    def test_partial_headroom_computes_correctly(self):
        result = self._run_block('{"headroom_fraction": 0.5, "label": "ok"}', "0.6")
        self.assertEqual(result.get("FLEET_SHARE_CEILING_PCT"), "30.0000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
