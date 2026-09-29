"""ask.py's filed issue clears both intake gates on arrival (jefe msg#162, philanthropy#8428)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask  # noqa: E402
import quality_gate  # noqa: E402
import vision_link_gate  # noqa: E402


class AskIssueGateLines(unittest.TestCase):
    def test_body_carries_acceptance_and_vision_link(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            if cmd[:3] == ["gh", "issue", "list"]:
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/issues/1\n")

        env = {"FLEET_ASK_ISSUES": "1", "FLEET_REPO_URL": "https://github.com/o/r"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            ask.issue_for_ask(7, "minion", "GSC key unset", "growth diagnosis", None, "credential", run=run)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        body = calls[-1][calls[-1].index("--body") + 1]
        self.assertGreaterEqual(quality_gate.count_gwt(body), 1)
        status, _ = vision_link_gate.classify_candidate(body, [])
        self.assertNotEqual(status, "MISSING")


class AskIssueSettles(unittest.TestCase):
    """jefe msgs #342/#351: a denied ask's mirror issue closes; an escalated one leaves the gates."""

    def settle(self, outcome, listed):
        calls = []

        def run(cmd):
            calls.append(cmd)
            if cmd[:3] == ["gh", "issue", "list"]:
                return SimpleNamespace(returncode=0, stdout=listed)
            return SimpleNamespace(returncode=0, stdout="")

        env = {"FLEET_ASK_ISSUES": "1", "FLEET_REPO_URL": "https://github.com/o/r"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            url = ask.settle_ask_issue(7, outcome, "the other way", run=run)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        return url, calls

    HIT = '[{"number": 9, "title": "ask #7 (credential): x", "url": "https://github.com/o/r/issues/9"}]'

    def test_denied_closes(self):
        url, calls = self.settle("denied", self.HIT)
        self.assertEqual(url, "https://github.com/o/r/issues/9")
        self.assertEqual(calls[-1][:4], ["gh", "issue", "close", "9"])

    def test_escalated_goes_human_op(self):
        _, calls = self.settle("escalated", self.HIT)
        edit = calls[1]
        self.assertEqual(edit[:4], ["gh", "issue", "edit", "9"])
        self.assertIn("fleet:needs-human-op", edit)
        self.assertFalse(any(c[:3] == ["gh", "issue", "close"] for c in calls))

    def test_prefix_twin_untouched(self):
        url, calls = self.settle("denied", '[{"number": 3, "title": "ask #70 (infra): y", "url": "u"}]')
        self.assertEqual(url, "")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
