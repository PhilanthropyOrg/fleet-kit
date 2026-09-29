"""2026-09-29: verified_test.sh runs the product's tests under eatmydata when it is installed.

On dino the tests sat blocked on fsync (throwaway SQLite, ~900 MB written per run) at ~17% CPU;
the same 109 tests took 1159s/1169s normally and 443s/464s with fsync off. A box without the
binary runs the tests exactly as before.

RED without the fix: the tests never run under eatmydata.
Run: python3 scripts/test_verified_test_eatmydata.py
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VT = ROOT / "scripts" / "verified_test.sh"

# Stand-in for the repo's diff-scoped runner: records whether eatmydata wrapped it.
FAKE_TFD = ("import os\n"
            "open(os.environ['RAN_LOG'], 'a').write(os.environ.get('UNDER_EMD', 'plain') + '\\n')\n")
FAKE_EMD = '#!/usr/bin/env bash\nexport UNDER_EMD=eatmydata\nexec "$@"\n'


def _repo() -> str:
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", d], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", d, "config", k, v], check=True)
    os.makedirs(os.path.join(d, "scripts"))
    pathlib.Path(d, "scripts", "tests_for_diff.py").write_text(FAKE_TFD)
    pathlib.Path(d, "x.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", d, "add", "-A"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


class EatMyData(unittest.TestCase):
    def _run(self, with_emd: bool) -> tuple[subprocess.CompletedProcess, str]:
        tmp = pathlib.Path(tempfile.mkdtemp())
        log = tmp / "ran.log"
        path = os.environ["PATH"]
        if with_emd:
            (tmp / "bin").mkdir()
            (tmp / "bin" / "eatmydata").write_text(FAKE_EMD)
            (tmp / "bin" / "eatmydata").chmod(0o755)
            path = f"{tmp / 'bin'}:{path}"
        else:  # hide a real eatmydata the test box may have
            path = ":".join(p for p in path.split(":")
                            if not (pathlib.Path(p) / "eatmydata").exists())
        d = _repo()
        r = subprocess.run(["bash", str(VT)], cwd=d, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, PATH=path, WT_PATH=d, RAN_LOG=str(log),
                                    FLEET_TEST_SLOT_DIR=str(tmp / "slots")))
        return r, log.read_text().strip() if log.exists() else ""

    def test_tests_run_under_eatmydata_when_installed(self):
        r, ran = self._run(with_emd=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(ran, "eatmydata")
        self.assertIn("fsync off for the test run", r.stdout)

    def test_missing_binary_runs_the_tests_as_before(self):
        r, ran = self._run(with_emd=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(ran, "plain")

    def test_image_installs_it(self):
        first_apt = (ROOT / "Dockerfile").read_text().split("apt-get install", 1)[1].split("&&", 1)[0]
        self.assertIn("eatmydata", first_apt.split(), "the image's apt-get install lacks eatmydata")


if __name__ == "__main__":
    unittest.main()
