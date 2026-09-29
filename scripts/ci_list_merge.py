#!/usr/bin/env python3
"""ci_list_merge.py -- settle a merge conflict in fleet-kit's ci.yml that is only two lists of
added test steps: keep both sides.

Every new scripts/test_*.py must get a `- name: run test_x.py` / `run: python3 scripts/test_x.py`
step (test_ci_runs_every_test_file.py), so two PRs that each add a test conflict on the same
spot. On 2026-09-29 that was 3 of the 4 fleet-kit PRs stuck 2-20h (#1392, #1436, #1445).

    python3 ci_list_merge.py .github/workflows/ci.yml

Exit 0: every conflict in the file was two added test-step lists, now both kept (file
rewritten). Exit 1: some conflict is anything else; the file is left exactly as it was.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HUNK = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n", re.S | re.M)
_STEP = re.compile(r"^\s+(- name: run test_[\w.]+\.py|run: python3 scripts/test_[\w.]+\.py)\s*$")


def only_test_steps(side: str) -> bool:
    lines = side.splitlines()
    return bool(lines) and all(_STEP.match(line) for line in lines)


def settle(text: str) -> str | None:
    """Both sides kept for every hunk, or None if any hunk is not two test-step lists. Pure."""
    hunks = list(_HUNK.finditer(text))
    if not hunks or not all(only_test_steps(h.group(1)) and only_test_steps(h.group(2)) for h in hunks):
        return None
    out = _HUNK.sub(lambda m: m.group(1) + m.group(2), text)
    return None if "<<<<<<<" in out or ">>>>>>>" in out else out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    out = settle(path.read_text())
    if out is None:
        print(f"ci_list_merge: {path} has a conflict that is not two lists of test steps -- left as is")
        return 1
    path.write_text(out)
    print(f"ci_list_merge: kept both sides' test steps in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
