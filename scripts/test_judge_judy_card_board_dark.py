"""A dark board costs a judge-judy pass one card budget, never more (PHIA-2276, 2026-11-02).

fleet-kit#1840 retried the card read (two tries of 150 s, then the agent name read the same way), so
one pass could wait 600 s on the board, and the app reviewer posted nothing for an hour after it merged.
These tests run judge-judy.sh's own card-read line against a board that takes the connection and never
answers, and check that the pass goes on within the budget, with the saved card or with none.
"""
import json
import os
import pathlib
import re
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
CARD_LINE = re.search(r"^\s*(timeout .*value_card\.py\" \"\$\{PR_TASK:-\}\".*)$", SRC, re.M).group(1)


class DarkBoardTest(unittest.TestCase):
    def setUp(self):
        # listen() with no accept(): the kernel completes the handshake, then nothing is ever sent
        self.board = socket.socket()
        self.board.bind(("127.0.0.1", 0))
        self.board.listen(16)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def card_step(self, cache, **env):
        card_file = self.dir / "body.card"
        full = dict(os.environ, KIT_DIR=str(ROOT), PR_TASK="PHIA-7", CARD_FILE=str(card_file),
                    LOG=str(self.dir / "judge-judy.log"), JUDGE_CARD_CACHE=str(cache),
                    PAPERCLIP_API_URL=f"http://127.0.0.1:{self.board.getsockname()[1]}",
                    PAPERCLIP_API_KEY="k", JUDGE_CARD_TIMEOUT="150", JUDGE_CARD_TRIES="2",
                    JUDGE_CARD_BUDGET="3", **env)
        start = time.monotonic()
        subprocess.run(["bash", "-c", CARD_LINE], env=full, check=True, timeout=60)
        return time.monotonic() - start, json.loads(card_file.read_text())

    def test_the_card_step_ends_within_its_budget_and_uses_the_saved_read(self):
        cache = self.dir / "cache"
        cache.mkdir()
        (cache / "PHIA-7.json").write_text(json.dumps(
            {"task": "PHIA-7", "card": "User: a donor", "owner": "jobs", "cached_at": "2026-11-02T08:00:00Z"}))
        took, card = self.card_step(cache)
        self.assertLess(took, 6)  # budget 3 s; two 150 s tries before PHIA-2276
        self.assertEqual(card["card"], "User: a donor")
        self.assertIn("read_failed", card)

    def test_no_saved_read_means_no_card_and_the_pass_goes_on(self):
        took, card = self.card_step(self.dir / "cache")
        self.assertLess(took, 6)
        self.assertFalse(card.get("card"))
        self.assertIn("error", card)

    def test_an_unwritable_cache_dir_is_not_fatal(self):
        blocker = self.dir / "file"
        blocker.write_text("")
        took, card = self.card_step(blocker / "cache")  # a folder under a file can never be made
        self.assertLess(took, 6)
        self.assertIn("error", card)

    def test_the_hard_stop_follows_the_budget_and_the_cache_sits_by_the_log(self):
        self.assertTrue(CARD_LINE.startswith('timeout "$(( ${JUDGE_CARD_BUDGET:-150} + 15 ))s" python3'))
        self.assertIn('JUDGE_CARD_CACHE="${JUDGE_CARD_CACHE:-$LOG_DIR/value-card-cache}"', SRC)
        self.assertIn("JUDGE_CARD_TIMEOUT=30 JUDGE_CARD_BUDGET=30 JUDGE_CARD_TRIES=1", SRC)


if __name__ == "__main__":
    unittest.main()
