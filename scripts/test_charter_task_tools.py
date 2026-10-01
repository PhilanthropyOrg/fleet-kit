"""A charter that opens with "call TaskCreate" must list TaskCreate in its spec's tools.allow.

RED without it: minion.md says "call TaskCreate ... with exactly these 11 items" but
minion.fleet.json allowed only Read/Edit/Write/Bash/Grep/Glob. Headless minions then found
nothing on ToolSearch and skipped the checklist; 7 of 13 minion self-critiques in the 6 hours
after the charter switch said "TaskCreate was not available", and no other member said it.

Run: python3 scripts/test_charter_task_tools.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

MEMBERS = Path(__file__).resolve().parent.parent / "members"


class CharterTaskTools(unittest.TestCase):
    def test_every_forced_plan_charter_is_allowed_the_task_tools(self):
        missing = []
        for spec_path in sorted(MEMBERS.glob("*/*.fleet.json")):
            spec = json.loads(spec_path.read_text())
            charter = spec_path.parent / spec.get("llm", {}).get("prompt_file", "")
            if not charter.is_file() or "call TaskCreate" not in charter.read_text():
                continue
            allow = spec["llm"].get("tools", {}).get("allow", [])
            for tool in ("TaskCreate", "TaskUpdate"):
                if tool not in allow:
                    missing.append(f"{spec_path.parent.name}: {tool}")
        self.assertEqual(missing, [], "charter says 'call TaskCreate' but tools.allow lacks it")


if __name__ == "__main__":
    sys.exit(unittest.main())
