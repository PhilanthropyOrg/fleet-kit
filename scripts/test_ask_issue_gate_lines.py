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


if __name__ == "__main__":
    unittest.main()
