"""run_member.sh must export the resolved maxx KEY even when the resolved HANDLE already
matches FLEET_MAXX_HANDLE (gh#1220).

RED without this fix: the old gate was `[ "$RESOLVED_HANDLE" != "${FLEET_MAXX_HANDLE:-}" ]` --
when fleet.env's own FLEET_MAXX_HANDLE already equals the resolved handle (true for
philanthropy: both are "philanthropy"), the block never runs, so FLEET_MAXX_KEY stays whatever
generic/stale value fleet.env set, never the per-account FLEET_MAXX_KEY_<ACCOUNT> value
resolve_maxx_handle.sh's own header says "MUST MOVE TOGETHER" with the handle.

Confirmed live 2026-09-22: bare `python3 maxx_reader.py` (fleet.env sourced only) returned
maxx_auth_rejected on the real box; the identical call with FLEET_MAXX_KEY overridden to
FLEET_MAXX_KEY_PHILANTHROPY returned a real reading. The handle already matched, so the old
"handle changed" guard never corrected the key -- the fk#1206/gh#1215 headroom-gauge fix was
live and correct, but every pass still auth-rejected before it ever got the chance to read.

This extracts and tests JUST the resolve block (lines ~339-363 of run_member.sh) via a small
shell harness with a stub resolve_maxx_handle.sh, rather than driving the whole 800+ line
script end to end.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _extract_resolve_block(run_member_text: str) -> str:
    start_marker = 'if [ "$DRY_RUN" -ne 1 ]; then\n  read -r RESOLVED_HANDLE RESOLVED_KEY'
    start = run_member_text.find(start_marker)
    assert start != -1, "resolve-handle block not found at its expected shape in run_member.sh"
    # Block ends at the matching top-level `fi` -- find the next blank-line-preceded `fi` at
    # column 0 after start (the block's own indentation is 2 spaces throughout, `fi` at col 0
    # closes it).
    end = run_member_text.find("\nfi\n", start)
    assert end != -1, "could not find the resolve-handle block's closing fi"
    return run_member_text[start:end + len("\nfi")]


class KeyFollowsHandleTest(unittest.TestCase):
    def setUp(self):
        self.run_member_text = (ROOT / "scripts" / "run_member.sh").read_text()
        self.block = _extract_resolve_block(self.run_member_text)

    def _run_block(self, resolved_handle: str, resolved_key: str,
                    existing_handle: str, existing_key: str) -> dict:
        """Runs the extracted block in a fresh bash, with a stub resolve_maxx_handle.sh, and
        returns the resulting FLEET_MAXX_HANDLE/FLEET_MAXX_KEY env."""
        with tempfile.TemporaryDirectory() as tmp:
            kit_dir = Path(tmp)
            scripts_dir = kit_dir / "scripts"
            scripts_dir.mkdir()
            stub = scripts_dir / "resolve_maxx_handle.sh"
            stub.write_text(f"#!/bin/bash\necho '{resolved_handle} {resolved_key}'\n")
            stub.chmod(0o755)

            script = f"""#!/bin/bash
set -uo pipefail
DRY_RUN=0
KIT_DIR={kit_dir}
LOG=/dev/null
MEMBER=test-member
FLEET_MAXX_HANDLE={existing_handle}
FLEET_MAXX_KEY={existing_key}
log() {{ :; }}
{self.block}
echo "FLEET_MAXX_HANDLE=$FLEET_MAXX_HANDLE"
echo "FLEET_MAXX_KEY=$FLEET_MAXX_KEY"
"""
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
            out = {}
            for line in proc.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    out[k] = v
            out["_stderr"] = proc.stderr
            out["_returncode"] = proc.returncode
            return out

    def test_key_updates_even_when_handle_already_matches(self):
        # The exact live bug shape: resolved handle == existing handle, but the resolved key
        # differs (the per-account key vs. fleet.env's generic one).
        result = self._run_block(
            resolved_handle="philanthropy", resolved_key="per-account-secret-key",
            existing_handle="philanthropy", existing_key="generic-stale-key",
        )
        self.assertEqual(result["FLEET_MAXX_HANDLE"], "philanthropy")
        self.assertEqual(result["FLEET_MAXX_KEY"], "per-account-secret-key",
                          f"key was not corrected when handle already matched: {result}")

    def test_handle_and_key_both_update_on_a_real_handle_change(self):
        result = self._run_block(
            resolved_handle="tgp", resolved_key="tgp-secret",
            existing_handle="reif", existing_key="reif-secret",
        )
        self.assertEqual(result["FLEET_MAXX_HANDLE"], "tgp")
        self.assertEqual(result["FLEET_MAXX_KEY"], "tgp-secret")

    def test_no_resolution_leaves_existing_values_untouched(self):
        result = self._run_block(
            resolved_handle="", resolved_key="",
            existing_handle="reif", existing_key="reif-secret",
        )
        self.assertEqual(result["FLEET_MAXX_HANDLE"], "reif")
        self.assertEqual(result["FLEET_MAXX_KEY"], "reif-secret")


if __name__ == "__main__":
    unittest.main(verbosity=2)
