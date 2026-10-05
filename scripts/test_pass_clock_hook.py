#!/usr/bin/env python3
"""A minion pass in its last 12 minutes is refused new waits and test runs, and told to report
(minion-item10862, 2026-10-04: killed 10 min after its pre-timeout checkpoint, mid `sleep 240`)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pass_clock_hook as h  # noqa: E402

NOW = 1_000_000.0
LATE = {"FLEET_RUN_ID": "minion-item10862-110677-1", "FLEET_PASS_DEADLINE": str(NOW + 600)}
EARLY = dict(LATE, FLEET_PASS_DEADLINE=str(NOW + 3600))


def test_late_minion_is_refused_the_waits_that_killed_10862() -> None:
    for cmd in ("cd /tmp/w; sleep 240; python3 /fleet-kit/scripts/pr_ci_wait.py 10920 2>&1 | head -4",
                "sleep 120",
                "python3 /fleet-kit/scripts/pr_ci_wait.py 10920 2>&1 | tail -15",
                "bash /fleet-kit/scripts/verified_test.sh 2>&1 | tail -6",
                "PYTHONPATH=src $PY -m pytest tests/test_x.py -q",
                "gh run watch 123",
                "gh pr checks 10920 --watch"):
        msg = h.decide(cmd, LATE, now=NOW)
        assert msg and "PASS CLOCK" in msg and "Report" in msg, cmd


def test_late_minion_can_still_finish_its_work() -> None:
    for cmd in ("git push -q origin HEAD",
                "gh pr comment 10920 --body 'blocked on the design files'",
                "python3 /fleet-kit/scripts/pr_ci_wait.py 10920 --no-wait",
                "sleep 5",
                "git commit -m 'note: sleep 300 is what killed it'",
                'git commit -m "pytest for the vendors cell"'):
        assert h.decide(cmd, LATE, now=NOW) is None, cmd


def test_inert_early_outside_minion_or_without_a_deadline() -> None:
    cmd = "sleep 240; python3 /fleet-kit/scripts/pr_ci_wait.py 10920"
    assert h.decide(cmd, EARLY, now=NOW) is None
    assert h.decide(cmd, dict(LATE, FLEET_RUN_ID="the-fixer-item10920-1-2"), now=NOW) is None
    assert h.decide(cmd, {"FLEET_RUN_ID": "minion-item1-2-3"}, now=NOW) is None
    assert h.decide(cmd, dict(LATE, FLEET_PASS_DEADLINE="junk"), now=NOW) is None


def test_hook_exit_codes_and_registration() -> None:
    import worktree_guard_hook_install as inst
    assert any("pass_clock_hook.py" in c for c in inst._hook_commands())
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sleep 240"}})
    env = {"PATH": "/usr/bin:/bin", "FLEET_RUN_ID": "minion-item1-2-3",
           "FLEET_PASS_DEADLINE": "1", "FLEET_LOG_DIR": "/tmp/pass-clock-test-logs"}
    r = subprocess.run([sys.executable, str(HERE / "pass_clock_hook.py")], input=payload,
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2 and "PASS CLOCK" in r.stderr
    r = subprocess.run([sys.executable, str(HERE / "pass_clock_hook.py")], input="not json",
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
