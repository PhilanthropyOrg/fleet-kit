"""Reif 2026-10-01: "readmes dont change - settings files can change".

README.md and RUNBOOK.md hold what stays true on every install. Which box, which paths, which
container and which logins are one install's facts; they live in instances/<name>/instance.env
(template: instance.env.example), which people load and scripts/fleet_status.sh reads.

This fails when one install's facts are written back into the docs, when the template carries
them, or when a doc uses a setting name the template does not define (a command nobody can run).
An issue reference (philanthropy#8215) is history, not an instance fact, and is allowed."""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "RUNBOOK.md")
EXAMPLE = ROOT / "instance.env.example"

# The first live install's facts. Nothing here is secret; it just is not true anywhere else.
INSTANCE_FACTS = ("dino", "luckymachines", "nonprofit-atlas", "/home/ubuntu", "philanthropy",
                  "thegoodproject", "reiftauati")
ISSUE_REF = re.compile(r"(PhilanthropyOrg/)?philanthropy#\d+")
# the caller's own fleet settings must not leak into the scripts under test
_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("FLEET_", "KIT_DIR"))}


def _facts_in(text: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        bare = ISSUE_REF.sub("", line).lower()
        hits += [f"{n}: {fact!r} in: {line.strip()[:100]}" for fact in INSTANCE_FACTS if fact in bare]
    return hits


def _where(kit: Path, env: dict | None = None) -> dict:
    out = subprocess.run(["bash", str(kit / "scripts" / "fleet_status.sh"), "--where"],
                         capture_output=True, text=True, check=True,
                         env={**_CLEAN_ENV, "HOME": "/home/nobody", **(env or {})})
    return dict(line.split("=", 1) for line in out.stdout.splitlines())


class DocsHoldWhatStaysTrue(unittest.TestCase):
    def test_docs_name_no_install(self):
        for name in DOCS:
            self.assertEqual(_facts_in((ROOT / name).read_text()), [],
                             f"{name} names one install; put it in instances/<name>/instance.env")

    def test_template_is_neutral(self):
        self.assertEqual(_facts_in(EXAMPLE.read_text()), [])

    def test_template_is_plain_settings(self):
        for line in EXAMPLE.read_text().splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^FLEET_BOX[A-Z_]*=\S*$")

    def test_every_setting_a_doc_uses_is_in_the_template(self):
        defined = set(re.findall(r"^(FLEET_BOX[A-Z_]*)=", EXAMPLE.read_text(), re.M))
        self.assertTrue(defined)
        for name in DOCS:
            used = set(re.findall(r"\$\{?(FLEET_BOX[A-Z_]*)", (ROOT / name).read_text()))
            self.assertTrue(used, f"{name} uses no instance setting at all")
            self.assertEqual(used - defined, set(), f"{name} uses settings the template lacks")

    def test_docs_point_at_the_roster_script_not_a_table(self):
        runbook = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("scripts/roster.sh", runbook)
        self.assertNotIn("| member | cadence", runbook)


class CodeReadsTheSameFile(unittest.TestCase):
    def _kit(self, tmp: str, settings: str | None) -> Path:
        kit = Path(tmp) / "kit"
        (kit / "scripts").mkdir(parents=True)
        (kit / "scripts" / "fleet_status.sh").write_text((ROOT / "scripts" / "fleet_status.sh").read_text())
        if settings is not None:
            (kit / "instances" / "acme").mkdir(parents=True)
            (kit / "instances" / "acme" / "instance.env").write_text(settings)
        return kit

    def test_status_script_reads_instance_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = _where(self._kit(tmp, EXAMPLE.read_text()))
        self.assertEqual(got["container"], "fleet-kit-myproduct")
        self.assertEqual(got["kit"], "/srv/fleet-kit")
        self.assertEqual(got["logs"], "/srv/fleet-kit/instances/myproduct/logs")
        self.assertEqual(got["deploy_log"], "/srv/fleet-kit/instances/myproduct/logs/auto_deploy.log")

    def test_environment_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = _where(self._kit(tmp, EXAMPLE.read_text()), {"FLEET_CONTAINER_NAME": "other"})
        self.assertEqual(got["container"], "other")

    def test_no_file_means_the_old_defaults(self):
        # the live box has no instance.env until its owner copies one there: nothing may change
        with tempfile.TemporaryDirectory() as tmp:
            got = _where(self._kit(tmp, None))
        self.assertEqual(got["settings"], "none (built-in defaults)")
        self.assertEqual(got["container"], "philanthropy")
        self.assertEqual(got["kit"], "/home/nobody/fleet-kit")
        self.assertEqual(got["logs"], "/home/nobody/fleet-kit/instances/nonprofit-atlas/logs")
        self.assertEqual(got["deploy_log"], "/home/nobody/fleet-kit-logs/auto_deploy.log")

    def test_two_instances_and_no_choice_means_the_old_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            kit = self._kit(tmp, EXAMPLE.read_text())
            (kit / "instances" / "b").mkdir()
            (kit / "instances" / "b" / "instance.env").write_text("FLEET_BOX_CONTAINER=b\n")
            self.assertEqual(_where(kit)["container"], "philanthropy")
            chosen = _where(kit, {"FLEET_INSTANCE_ENV": str(kit / "instances" / "b" / "instance.env")})
        self.assertEqual(chosen["container"], "b")


class RosterComesFromTheSpecs(unittest.TestCase):
    def _roster(self, tmp: str) -> str:
        # HOME is the temp dir too: overrides.py copies a legacy store forward from $HOME
        return subprocess.run(["bash", str(ROOT / "scripts" / "roster.sh")], capture_output=True,
                              text=True, check=True,
                              env={**_CLEAN_ENV, "HOME": tmp, "FLEET_LOG_DIR": tmp}).stdout

    def test_roster_lists_every_spec_and_says_overrides_are_not_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._roster(tmp)
        for spec in sorted((ROOT / "members").glob("*/*.fleet.json")):
            self.assertIn(f"| {spec.parent.name} |", out)
        self.assertIn("| gru | yes | hourly :03 |", out)
        self.assertIn("Live overrides: NOT shown", out)

    def test_roster_marks_a_live_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {"member": "gru", "key": "enabled", "value": False, "by": "test", "why": "test",
                   "set_at": time.time(), "expires_at": time.time() + 3600}
            (Path(tmp) / "fleet-overrides.jsonl").write_text(json.dumps(row) + "\n")
            out = self._roster(tmp)
        self.assertIn("| gru | no* |", out)
        self.assertIn("Live overrides: applied from", out)


if __name__ == "__main__":
    unittest.main()
