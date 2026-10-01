"""The plain-words rewrite is a 160-word text call, not a full agent session.

Live fleet, 2026-09-24..10-01, read from the CLI's own transcripts: 4,821 rewrite sessions,
$328 at list price, none of it in runs.jsonl. Each session loaded the product repo's
SessionStart hooks, its CLAUDE.md, the skill list and 12 tool definitions, then thought for
4,000-10,000 output tokens to write ~110. A session took 50-100s against run_mail's 120s
timeout and cost $0.05-0.08 against its $0.05 cap: through 09-28 not one run got a `plain`
field, and on 10-01 142 of 573 still timed out.

The fake `claude` here stands in for that: a call that brings the agent baggage is slow (it
outlives the timeout), a call without it answers at once.

Run: python3 scripts/test_plain_rewrite_lean.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_mail  # noqa: E402

REC = {"member": "minion", "run_id": "minion-item7-1-1", "status": "ok",
       "outcome": "Opened PR #12", "evidence": "gh pr view 12"}
PLAIN = "The helper opened a change.\n\nWhat this means: it is ready.\n\nWhat to do: Nothing -- this is just a receipt."

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
if argv[:1] == ["--help"]:
    sys.stdout.write(open(os.environ["FAKE_CLAUDE_HELP"]).read())
    sys.exit(0)
def after(flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None
lean = (after("--setting-sources") == "user" and "--disable-slash-commands" in argv
        and after("--tools") == "" and os.environ.get("MAX_THINKING_TOKENS") == "0")
with open(os.environ["FAKE_CLAUDE_CALLS"], "a") as f:
    f.write(json.dumps({"argv": argv, "lean": lean, "thinking": os.environ.get("MAX_THINKING_TOKENS"),
                        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
if not lean:
    time.sleep(float(os.environ["FAKE_CLAUDE_SLOW_S"]))  # hooks, tools, thousands of thinking tokens
print(os.environ["FAKE_CLAUDE_TEXT"])
'''

# The lines of `claude --help` (CLI 2.1.284, the live box) that matter here.
HELP_NEW = """Usage: claude [options] [command] [prompt]

Options:
  --disable-slash-commands              Disable all skills
  --max-budget-usd <amount>             Maximum dollar amount to spend on API
  -p, --print                           Print response and exit
  --setting-sources <sources>           Comma-separated list of setting sources
                                        to load (user, project, local).
  --tools <tools...>                    Specify the list of available tools from
                                        the built-in set. Use "" to disable all
                                        tools
"""
# An older CLI: no --tools entry of its own, only a mention inside another flag's description.
HELP_OLD = """Usage: claude [options] [command] [prompt]

Options:
  --max-budget-usd <amount>             Maximum dollar amount to spend on API
  -p, --print                           Print response and exit
  --restricted                          Restricted mode: removes Bash unless
                                        --tools names them, and ignores user,
                                        project and local settings files
  --setting-sources <sources>           Comma-separated list of setting sources
"""


class Box:
    """A temp box: fake `claude` first on PATH, a three-account pool, its own log dir."""

    def __init__(self, case: unittest.TestCase, help_text: str = HELP_NEW, **extra: str):
        self.dir = Path(tempfile.mkdtemp())
        case.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)]))
        (self.dir / "bin").mkdir()
        fake = self.dir / "bin" / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        (self.dir / "help.txt").write_text(help_text)
        self.calls = self.dir / "calls.jsonl"
        self.env = {
            "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "HOME": str(self.dir),
            "FLEET_LOG_DIR": str(self.dir), "FLEET_ACCOUNTS": "one two three",
            "ACCOUNT_POOL_LOG_FILE": str(self.dir / "account-pool.log"),
            "FAKE_CLAUDE_HELP": str(self.dir / "help.txt"), "FAKE_CLAUDE_CALLS": str(self.calls),
            "FAKE_CLAUDE_SLOW_S": "30", "FAKE_CLAUDE_TEXT": PLAIN,
            "FLEET_MAXX_URL": "", "CLAUDE_CODE_OAUTH_TOKEN": "", **extra}

    def seen(self) -> list[dict]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def plain_words(self) -> tuple[str, list[dict]]:
        log = self.dir / "plain-words.log"
        with unittest.mock.patch.dict(os.environ, self.env), \
             unittest.mock.patch.object(run_mail, "PLAIN_LOG_FILE", log), \
             unittest.mock.patch.object(run_mail, "PLAIN_TIMEOUT_S", 8), \
             unittest.mock.patch.object(run_mail, "_lean_ok", None, create=True):
            text = run_mail.plain_words(REC)
        fails = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return text, fails


class RewriteIsALeanCall(unittest.TestCase):
    def test_the_run_gets_its_plain_words_in_one_call(self):
        """THE WASTE: before this change the rewrite ran with the agent baggage, outlived its
        timeout and the run got no `plain` field -- paid for, nothing kept."""
        box = Box(self)
        text, fails = box.plain_words()
        self.assertEqual(text, PLAIN, f"no plain words; plain-words.log says {fails}")
        self.assertEqual(fails, [])
        self.assertEqual(len(box.seen()), 1, "one run, one rewrite call -- no failover re-runs")

    def test_the_call_drops_hooks_skills_tools_and_thinking_and_nothing_else(self):
        box = Box(self)
        box.plain_words()
        call = box.seen()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")  # no project hooks / CLAUDE.md
        self.assertIn("--disable-slash-commands", argv)                      # no skill list
        self.assertEqual(argv[argv.index("--tools") + 1], "")                # no tools
        self.assertEqual(call["thinking"], "0")                              # no thinking
        # Unchanged: the prompt, the model, the output format, the cap, the account pool.
        self.assertEqual(argv[:2], ["-p", run_mail.PLAIN_PROMPT + run_mail.raw_text(REC)])
        self.assertEqual(argv[argv.index("--model") + 1], run_mail.PLAIN_MODEL)
        self.assertEqual(argv[argv.index("--output-format") + 1], "text")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], run_mail.PLAIN_BUDGET_USD)
        self.assertTrue(call["config_dir"], "the call still goes through account_pool_run")

    def test_the_switch_puts_the_old_call_back(self):
        box = Box(self, FLEET_RUN_PLAIN_LEAN="0")
        text, fails = box.plain_words()
        call = box.seen()[0]
        self.assertFalse(call["lean"])
        self.assertNotIn("--tools", call["argv"])
        self.assertIsNone(call["thinking"])
        # ...and the old call is the one the live fleet timed out on.
        self.assertEqual((text, [f["reason"] for f in fails]), ("", ["timeout"]))

    def test_a_cli_without_the_flags_gets_the_old_call_not_a_failing_one(self):
        """An unknown flag fails on every account, and the pool counts each failure against an
        account the members run on. `--tools` named inside another flag's text is not the flag."""
        box = Box(self, help_text=HELP_OLD, FAKE_CLAUDE_SLOW_S="0")
        text, fails = box.plain_words()
        self.assertEqual((text, fails), (PLAIN, []))
        calls = box.seen()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--tools", calls[0]["argv"])
        self.assertNotIn("--disable-slash-commands", calls[0]["argv"])
        pool_log = box.dir / "account-pool.log"
        self.assertNotIn("failed", pool_log.read_text() if pool_log.exists() else "")


class TheRunRecordCarriesIt(unittest.TestCase):
    def test_run_report_writes_the_plain_field(self):
        """As the fleet uses it: run_member.sh pipes the pass text into run_report.py."""
        box = Box(self)
        env = {**os.environ, **box.env, "FLEET_RUN_PLAIN": "1", "FLEET_RUN_MAIL_TIMEOUT_S": "8"}
        p = subprocess.run([sys.executable, str(HERE / "run_report.py"), "--member", "minion",
                            "--run-id", "minion-item7-1-1", "--pass-file", "-"],
                           input="Outcome: Opened PR #12\nEvidence: gh pr view 12\n",
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = json.loads(p.stdout)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec.get("plain"), PLAIN)
        self.assertEqual(len(box.seen()), 1)


if __name__ == "__main__":
    unittest.main()
