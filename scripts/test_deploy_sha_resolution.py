"""Regression test for deploy.sh/up.sh (fleet-kit#990, fleet-kit#1000, philanthropy gh#5848).

Both image-build call sites used to fall back to the literal string "unknown" whenever
`git rev-parse HEAD` failed (`|| echo unknown`), so a build that could not identify its own
commit still shipped and got tagged. The only symptom was deploy_staleness_check.log logging
"no .deploy_sha baked into this image" on every hourly tick with zero build-time trace -- live
for 24h+ straight through 2026-09-14 (see deploy_staleness_check.log).

This test exercises the actual `resolve_deploy_sha` shell function (extracted from deploy.sh and
run for real under bash, not just grepped for), red then green:
  RED:   KIT_DIR pointed at a directory with no .git -> non-zero exit, no stdout.
  GREEN: KIT_DIR pointed at this real checkout -> exit 0, output is a 40-char lowercase hex sha
         matching `git rev-parse HEAD` run directly.

It also checks, at the source level, that neither deploy.sh's two build call sites nor up.sh's
build call site still contain the silent `|| echo unknown` fallback, and that both deploy.sh
call sites bail out (`return 1` / `exit 1`) before their `podman build` line ever runs.

Run: python3 scripts/test_deploy_sha_resolution.py
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
DEPLOY_SH = KIT / "scripts" / "deploy.sh"
UP_SH = KIT / "up.sh"
DEPLOY = DEPLOY_SH.read_text()
UP = UP_SH.read_text()

REAL_HEAD_SHA = subprocess.run(
    ["git", "-C", str(KIT), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
).stdout.strip()


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}", start)
    return source[start:end + 2]


class ResolveDeploySHAExecutionTests(unittest.TestCase):
    """Runs the real function under bash -- not a source-text guess."""

    def _run(self, kit_dir: str) -> subprocess.CompletedProcess:
        func_src = _extract_function(DEPLOY, "resolve_deploy_sha")
        script = f'KIT_DIR="{kit_dir}"\n{func_src}\nresolve_deploy_sha\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_red_non_git_dir_fails_loud_with_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp)
        self.assertNotEqual(result.returncode, 0,
                             "resolve_deploy_sha must fail (non-zero exit) when KIT_DIR has no .git")
        self.assertEqual(result.stdout.strip(), "",
                          "a failed resolution must not print a fake/partial sha")

    def test_green_real_checkout_returns_real_head_sha(self):
        result = self._run(str(KIT))
        self.assertEqual(result.returncode, 0)
        sha = result.stdout.strip()
        self.assertRegex(sha, r"^[0-9a-f]{40}$", f"not a 40-char lowercase hex sha: {sha!r}")
        self.assertEqual(sha, REAL_HEAD_SHA)


_SILENT_FALLBACK = re.compile(r"rev-parse HEAD 2>/dev/null \|\| echo unknown")


class NoSilentFallbackTests(unittest.TestCase):
    def test_deploy_sh_has_no_echo_unknown_fallback(self):
        self.assertIsNone(_SILENT_FALLBACK.search(DEPLOY),
                           "deploy.sh still silently falls back to the literal string 'unknown'")

    def test_up_sh_has_no_echo_unknown_fallback(self):
        self.assertIsNone(_SILENT_FALLBACK.search(UP),
                           "up.sh still silently falls back to the literal string 'unknown'")


class BuildOrderingTests(unittest.TestCase):
    """Confirms the failure path in each build call site is reached BEFORE `podman build`,
    i.e. an unresolved DEPLOY_SHA can never reach a podman build/tag."""

    def test_proxy_deploy_bails_before_podman_build(self):
        body = _extract_function(DEPLOY, "proxy_deploy")
        resolve_pos = body.index("resolve_deploy_sha")
        build_pos = body.index("podman build")
        bail_pos = body.index("return 1", resolve_pos)
        self.assertLess(resolve_pos, bail_pos)
        self.assertLess(bail_pos, build_pos)

    def test_legacy_path_bails_before_podman_build(self):
        resolve_pos = DEPLOY.rindex("resolve_deploy_sha")
        build_pos = DEPLOY.index("podman build --build-arg DEPLOY_SHA=\"$DEPLOY_SHA\"")
        bail_pos = DEPLOY.index("exit 1", resolve_pos)
        self.assertLess(resolve_pos, bail_pos)
        self.assertLess(bail_pos, build_pos)

    def test_up_sh_bails_before_podman_build(self):
        resolve_pos = UP.index("UP_DEPLOY_SHA=")
        build_pos = UP.index("podman build")
        bail_pos = UP.index("exit 1", resolve_pos)
        self.assertLess(resolve_pos, bail_pos)
        self.assertLess(bail_pos, build_pos)


if __name__ == "__main__":
    unittest.main()
