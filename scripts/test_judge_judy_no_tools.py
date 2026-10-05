"""judge-judy's claude call runs with no tools (2026-10-02).

The reviewer reads the diff as untrusted TEXT, and judge-judy.fleet.json denies every tool, but
the `claude -p` call passed no tool flag: 239 of 393 reviews in a day used Read/Grep on the repo,
and every call carried ~19k tokens of tool definitions it does not need.
"""
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
SPEC = json.loads((ROOT / "members" / "judge-judy" / "judge-judy.fleet.json").read_text())


class NoToolsTest(unittest.TestCase):
    def test_the_review_call_disables_every_tool(self):
        calls = re.findall(r'exec claude -p "\$@" < "\$JJ_PROMPT_FILE".*\n.*', SRC)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn('--tools ""', calls[0])
        self.assertIn("--json-schema", calls[0])

    def test_the_spec_still_denies_the_repo_tools(self):
        deny = SPEC["llm"]["tools"]["deny"]
        for tool in ("Read", "Grep", "Glob", "Bash"):
            self.assertIn(tool, deny)


if __name__ == "__main__":
    unittest.main()
