"""philanthropy#8707: gru never drops a prod alert for missing a spec.

#8695 ('prod down: .../990/health/canary', filed by prod_health_check.py mid-outage) was skipped
at gru's intake as "missing at least one Given/When/Then acceptance criterion". Two sides:
  1. every auto-filer's issue carries a G/W/T the gate's OWN parser accepts, a Vision-link and
     exactly one quality:* label -- checked against quality_gate / vision_link_gate, not a regex
     copy of them;
  2. the gate treats an incident-labeled issue as spec-complete even with no hand-written spec.

Run: python3 scripts/test_alert_spec_8707.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixer_fire_path  # noqa: E402
import gate_drops  # noqa: E402
import inbox  # noqa: E402
import prod_health_check as phc  # noqa: E402
import prod_incident  # noqa: E402
import quality_gate  # noqa: E402
import vision_link_gate  # noqa: E402


def _parts(cmd: list[str]) -> tuple[str, list[str]]:
    body = cmd[cmd.index("--body") + 1]
    labels = [lb for i, tok in enumerate(cmd) if tok == "--label" for lb in cmd[i + 1].split(",")]
    return body, labels


class FilerCarriesSpec(unittest.TestCase):
    def assert_spec_complete(self, cmd: list[str]):
        body, labels = _parts(cmd)
        # The gate's own parsers, on the filer's real output. Alert labels stripped so this
        # proves the body clears the gate on its own, not via the label bypass below.
        plain = [lb for lb in labels if lb not in ("incident", "fleet:incident")]
        self.assertGreaterEqual(quality_gate.count_gwt(body), 1, body)
        self.assertEqual(vision_link_gate.classify_candidate(body, [])[0],
                         vision_link_gate.STATUS_MAINTENANCE, body)
        self.assertEqual(len(set(plain) & set(quality_gate.QUALITY_LABELS)), 1, labels)
        ok, reason = quality_gate.classify_candidate([{"name": lb} for lb in plain], body, [])
        self.assertTrue(ok, reason)

    def test_prod_health_check_heartbeat_ticket(self):
        self.assert_spec_complete(phc.build_file_cmd("prod down: https://x/990/health/canary",
                                                     "Detected by prod_health_check.py"))

    def test_prod_incident_shared_filer(self):
        self.assert_spec_complete(prod_incident.build_create_cmd(
            "O/R", "prod down: https://x/990", "body", phc.INCIDENT_LABELS, target="https://x/990"))

    def test_fixer_fire_path(self):
        self.assert_spec_complete(fixer_fire_path.build_incident_create_cmd("PROD DOWN", "body"))

    def test_inbox_alert_intake(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            class R:
                returncode, stdout, stderr = 0, "[]" if cmd[:3] == ["gh", "issue", "list"] \
                    else "https://github.com/O/R/issues/1\n", ""
            return R()
        inbox.repo_slug = lambda: "O/R"
        inbox.file_or_comment_alert({"check": "pg_health:disk", "text": "disk 95%"}, run=run)
        create = next(c for c in calls if c[:3] == ["gh", "issue", "create"])
        self.assert_spec_complete(create)


class GateAcceptsAlert(unittest.TestCase):
    ALERT = {"number": 8695, "labels": [{"name": "incident"}, {"name": "fleet:backlog"},
                        {"name": "quality:solid"}],
             "body": "Detected by `prod_health_check.py` running on dino.", "comments": []}

    def test_quality_gate_accepts_alert_without_spec(self):
        ok, reason = quality_gate.classify_candidate(self.ALERT["labels"], self.ALERT["body"], [])
        self.assertTrue(ok, reason)

    def test_intake_plan_keeps_alert_eligible(self):
        out = gate_drops.plan([dict(self.ALERT)], "test-8707")
        self.assertEqual(out["eligible"], [8695], out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
