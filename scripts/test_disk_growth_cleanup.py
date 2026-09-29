"""Regression test: the two things that refilled dino's disk get cleaned up (2026-09-28).

1. deploy.sh builds a fresh ~2.3GB image per deploy and never pruned the dangling previous one
   (65 of them on dino). finish_deploy must run `podman image prune`.
2. test_python.sh builds a ~900MB venv per dependency hash and never evicted old ones (5 dead
   keys, 4.1GB). A new build must evict siblings unused for a day, and keep recently-used ones.

Both are exercised for real with a stub podman/uv on PATH.

Run: python3 scripts/test_disk_growth_cleanup.py
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


class DeployPrunesDanglingImages(unittest.TestCase):
    def test_finish_deploy_prunes(self):
        src = (KIT / "scripts" / "deploy.sh").read_text()
        def fn(name: str) -> str:
            i = src.find(f"\n{name}() {{\n")
            return "" if i < 0 else src[i + 1: src.index("\n}\n", i) + 3]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            calls = d / "podman.calls"
            _stub(d, "podman", f'echo "$@" >> {calls}\n')
            script = "log() { :; }\n" + fn("prune_dangling_images") + fn("finish_deploy") + "finish_deploy\nwait\nsleep 0.3\n"
            subprocess.run(["bash", "-c", script], env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}",
                           "CONTAINER": "c", "RETIRED_MARKER": "c-retired"}, check=True, timeout=30)
            self.assertIn("image prune -f", calls.read_text())


class TestVenvEviction(unittest.TestCase):
    def test_new_build_evicts_stale_keeps_recent(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bindir, root, wt = d / "bin", d / "venvs", d / "wt"
            for p in (bindir, root, wt):
                p.mkdir()
            (wt / "pyproject.toml").write_text('[project]\nname="x"\ndependencies=[]\n')
            # uv venv ... <dir>  -> make <dir>/bin/python that exits 0; uv pip install -> ok.
            _stub(bindir, "uv", 'if [ "$1" = venv ]; then for a; do v="$a"; done; mkdir -p "$v/bin"; '
                                'printf "#!/bin/sh\\nexit 0\\n" > "$v/bin/python"; chmod +x "$v/bin/python"; fi\nexit 0\n')
            stale, recent = root / "aaaaaaaaaaaaaaaa", root / "bbbbbbbbbbbbbbbb"
            for v in (stale, recent):
                (v / "bin").mkdir(parents=True)
                (v / ".ready").touch()
            old = time.time() - 2 * 86400
            os.utime(stale / ".ready", (old, old))
            (root / "pythons").mkdir()
            out = subprocess.run(["bash", str(KIT / "scripts" / "test_python.sh"), str(wt)], capture_output=True, text=True,
                                 env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FLEET_TEST_VENV_ROOT": str(root)},
                                 timeout=60)
            self.assertIn(str(root), out.stdout, out.stderr)
            self.assertFalse(stale.exists(), "stale venv (unused 2 days) was not evicted")
            self.assertTrue(recent.exists(), "recently-used venv was evicted")
            self.assertTrue((root / "pythons").exists(), "shared interpreter dir was evicted")


if __name__ == "__main__":
    unittest.main()
