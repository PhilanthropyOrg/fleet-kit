"""site_access_pull.py + the probe's `access-log` verb: the fleet keeps the WHOLE nginx access log
(Reif 2026-10-01: "we need the whole thing"). Drives the real prod_runtime_probe.sh against a
fake log on disk, so the copy is checked byte for byte through growth, a half-written line,
nginx's daily rotation, and a dropped connection."""
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import site_access_pull as sap  # noqa: E402

PROBE = HERE / "prod_runtime_probe.sh"
NOW = 1790841600  # 2026-10-01T08:00:00Z


class AccessLogPull(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.log = self.d / "access.log"
        self.out = self.d / "site_access"
        self.log.write_bytes(b"")

    def tearDown(self):
        shutil.rmtree(self.d)

    def cmd(self, ino, off):
        return ["env", f"SSH_ORIGINAL_COMMAND=access-log {ino} {off}", f"PROBE_ACCESS_LOG={self.log}",
                "PROBE_ENV_FILE=/nonexistent", "bash", str(PROBE)]

    def write(self, b):
        with open(self.log, "ab") as f:
            f.write(b)

    def copy(self):
        return b"".join(gzip.open(p).read() for p in sorted(self.out.glob("access-*.log.gz")))

    def test_whole_log_arrives_once_through_growth_partial_line_and_rotation(self):
        self.write(b"GET /a 200\nGET /b 404\n")
        sap.pull(self.cmd, self.out, NOW)
        self.assertEqual(self.copy(), b"GET /a 200\nGET /b 404\n")

        self.write(b"GET /c 200\nGET /d")          # nginx mid-write: half a line
        sap.pull(self.cmd, self.out, NOW + 300)
        self.assertEqual(self.copy(), b"GET /a 200\nGET /b 404\nGET /c 200\n", "half line must wait")

        sap.pull(self.cmd, self.out, NOW + 400)    # nothing new: nothing doubled
        self.write(b" 500\nGET /e 200\n")          # rest of the line, then nginx rotates
        self.log.rename(self.d / "access.log.1")
        self.log.write_bytes(b"GET /f 301\n")
        sap.pull(self.cmd, self.out, NOW + 600)
        self.assertEqual(self.copy(), b"GET /a 200\nGET /b 404\nGET /c 200\nGET /d 500\nGET /e 200\nGET /f 301\n")

    def test_dropped_connection_saves_nothing_and_next_pull_catches_up(self):
        self.write(b"GET /a 200\n")
        sap.pull(self.cmd, self.out, NOW)
        self.write(b"GET /b 200\n")
        with self.assertRaises(SystemExit):
            sap.pull(lambda i, o: ["sh", "-c", "printf '#fleet-access-log ino=1 end=9\\nGET' | gzip -c; exit 255"], self.out, NOW + 300)
        sap.pull(self.cmd, self.out, NOW + 600)
        self.assertEqual(self.copy(), b"GET /a 200\nGET /b 200\n")

    def test_sign_in_tokens_never_reach_the_copy(self):
        self.write(b'1.2.3.4 - [01/Oct/2026] "GET /990/auth/magic?token=s3cr3tT0k HTTP/2.0" 302 0 "-" "UA"\n'
                   b'"GET /network/hq/messages/ask/confirm?id=9&token=abc HTTP/2.0" 200\n'
                   b'"GET /990/claim/1?placement=report&t=x HTTP/2.0" 200\n')
        sap.pull(self.cmd, self.out, NOW)
        got = self.copy()
        self.assertNotIn(b"s3cr3tT0k", got)
        self.assertNotIn(b"token=abc", got)
        self.assertIn(b"/990/auth/magic?token=REDACTED HTTP/2.0\" 302", got)
        self.assertIn(b"placement=report&t=x", got, "non-secret params stay for pattern work")

    def test_old_days_are_dropped(self):
        self.out.mkdir()
        old = self.out / "access-2026-09-01.log.gz"
        old.write_bytes(gzip.compress(b"x\n"))
        sap.pull(self.cmd, self.out, NOW)
        self.assertFalse(old.exists())

    def test_probe_refuses_anything_but_two_numbers(self):
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": "access-log 1 2; cat /etc/passwd", "PROBE_ACCESS_LOG": str(self.log)}
        r = subprocess.run(["bash", str(PROBE)], env=env, capture_output=True)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, b"")


if __name__ == "__main__":
    unittest.main()
