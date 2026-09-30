"""Home sat on "loading..." for ~50s on 2026-09-30: /api/metrics's scoreboard entries live 30
min in _cached(), nothing refreshed them, so whoever opened the dashboard after they expired
paid the whole gh rebuild with a blank page.

RED before this change: there was no refresh-ahead mode, so a viewer arriving while an entry
was being rebuilt waited on the lock for the rebuild.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
(TMP / "fleet.env").write_text("")
os.environ.update(FLEET_ENV_FILE=str(TMP / "fleet.env"), FLEET_LOG_DIR=str(TMP), FLEET_REPO=str(TMP))
sys.path.insert(0, str(KIT / "scripts"))
import fleet_view_server as fvs  # noqa: E402


class RefreshAhead(unittest.TestCase):
    def setUp(self):
        fvs._TTL_CACHE.clear()

    def test_viewer_reads_old_value_while_refresher_rebuilds(self):
        fvs._TTL_CACHE["k"] = (time.time() - 1000, "old")   # past half of a 1800s TTL
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return "new", True

        def refresher():
            fvs._REFRESH.ahead = True
            fvs._cached("k", 1800, slow)

        t = threading.Thread(target=refresher)
        t.start()
        self.assertTrue(started.wait(2), "refresher did not rebuild an entry past half its TTL")
        t0 = time.time()
        got = fvs._cached("k", 1800, lambda: ("viewer rebuilt", True))
        waited = time.time() - t0
        release.set()
        t.join()
        self.assertEqual(got, "old")
        self.assertLess(waited, 1, "viewer waited on the refresher's rebuild")
        self.assertEqual(fvs._TTL_CACHE["k"][1], "new")

    def test_refresher_leaves_fresh_entry_alone(self):
        fvs._TTL_CACHE["k"] = (time.time() - 10, "fresh")
        fvs._REFRESH.ahead = True
        try:
            self.assertEqual(fvs._cached("k", 1800, lambda: ("rebuilt", True)), "fresh")
        finally:
            fvs._REFRESH.ahead = False


if __name__ == "__main__":
    unittest.main()
