"""A reader of fleet.db must not wait on a member's open write.

RED without WAL: in the rollback journal a writer mid-transaction locks readers out, so the
dashboard's reads queued behind member writes and /api/metrics failed "database is locked"
after 17s (dino, 2026-09-26). GREEN with WAL: the read returns at once, from the last commit.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_db  # noqa: E402


class ReaderDoesNotWaitOnWriter(unittest.TestCase):
    def test_read_during_open_write(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "fleet.db"
            fleet_db.connect(db).close()
            writer = sqlite3.connect(str(db), timeout=0.1, isolation_level=None)
            writer.execute("BEGIN EXCLUSIVE")  # a member mid-write, as they are all day
            writer.execute("UPDATE sync_state SET offset = 2 WHERE id = 0")
            reader = sqlite3.connect(str(db), timeout=0.1)
            try:
                self.assertEqual(reader.execute("SELECT offset FROM sync_state WHERE id = 0").fetchone(), (0,))
            finally:
                writer.execute("ROLLBACK")
                writer.close()
                reader.close()


if __name__ == "__main__":
    unittest.main()
