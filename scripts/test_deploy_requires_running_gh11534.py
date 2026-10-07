"""gh#11534: deploy.sh must not print DEPLOYED while the live container is not running.

No podman in the sandbox, so this reads the script: both cutover paths call the running check
before finish_deploy, and the check starts the container once and fails (rollback) if it still
is not running.

Run: python3 scripts/test_deploy_requires_running_gh11534.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

DEPLOY = (Path(__file__).resolve().parent / "deploy.sh").read_text()


class DeployedNeedsRunningTests(unittest.TestCase):
    def test_proxy_path_checks_before_finish_and_rolls_back(self):
        i = DEPLOY.index("proxy_deploy || exit 1")
        tail = DEPLOY[i : DEPLOY.index("finish_deploy", i)]
        self.assertIn("live_container_running_or_start || { proxy_rollback; exit 1; }", tail)

    def test_legacy_path_checks_before_finish_and_rolls_back(self):
        i = DEPLOY.rindex("if ! live_container_running_or_start")
        block = DEPLOY[i : DEPLOY.rindex("\nfinish_deploy")]
        self.assertIn("ROLLED BACK", block)
        self.assertIn("exit 1", block)

    def test_check_starts_once_and_logs_the_state(self):
        i = DEPLOY.index("live_container_running_or_start() {")
        body = DEPLOY[i : DEPLOY.index("finish_deploy() {")]
        self.assertIn("{{.State.Status}}", body)
        self.assertEqual(body.count('podman start "$CONTAINER"'), 1)
        self.assertIn("DEPLOY FAILED", body)


if __name__ == "__main__":
    unittest.main()
