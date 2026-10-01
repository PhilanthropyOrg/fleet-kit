"""Tests for /webhook/box-log (receiver) and box_log_incidents.py (host side) -- nonprofit-atlas#8703.

Job: every prod-box log line lands in ONE store on dino; an ERROR/ALERT/non-zero exit becomes ONE
board item per incident key, repeats never twin it, and the job's next clean run closes it.

Run: python3 scripts/test_box_log.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import box_log_incidents as bi  # noqa: E402
import webhook_receiver as wr  # noqa: E402


def _rec(**kw):
    r = {"ts": "2026-09-29T05:06:50Z", "host": "990-Lookup", "source": "file",
         "job": "prune_api_cache", "level": "INFO", "msg": "x"}
    r.update(kw)
    return r


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "box-logs.jsonl"
        for name, val in (("BOX_LOGS_FILE", self.store), ("LOG_FILE", Path(self.tmp.name) / "x.log")):
            p = mock.patch.object(wr, name, val)
            p.start(); self.addCleanup(p.stop)
        env = mock.patch.dict("os.environ", {"FLEET_WEBHOOK_TOKENS": "atlas-prod-alerts:tok-123"})
        env.start(); self.addCleanup(env.stop)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), wr.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def post(self, body, token="tok-123", key="k1"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.srv.server_address[1]}/webhook/box-log",
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_batch_lands_structured_and_retry_is_deduped(self):
        batch = {"host": "990-Lookup", "records": [
            _rec(level="EXIT", exit=1, key="exit:prune_api_cache", run_keys=["exit:prune_api_cache"]),
            _rec(level="bogus", key="bad key with spaces")]}
        self.assertEqual(self.post(batch), (200, {"ok": True, "stored": 2, "duplicate": False}))
        self.assertEqual(self.post(batch)[1]["duplicate"], True)  # lost-ack retry: same key
        rows = [json.loads(ln) for ln in self.store.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["exit"], rows[0]["key"], rows[0]["caller"]),
                         (1, "exit:prune_api_cache", "atlas-prod-alerts"))
        self.assertEqual(rows[1]["level"], "INFO")
        self.assertNotIn("key", rows[1])

    def test_bad_token_and_bad_shape_rejected(self):
        self.assertEqual(self.post({"records": []}, token="nope")[0], 401)
        self.assertEqual(self.post({"records": "x"})[0], 400)
        self.assertFalse(self.store.exists())

    def test_rotates_past_max_bytes(self):
        with mock.patch.object(wr, "BOX_LOGS_MAX_BYTES", 10):
            for i in range(3):
                wr.record_box_logs([_rec(msg=f"line {i}")], f"key{i}")
        self.assertIn("line 2", self.store.read_text())
        self.assertIn("line 1", self.store.with_name("box-logs.jsonl.1").read_text())
        self.assertIn("line 0", self.store.with_name("box-logs.jsonl.2").read_text())


class FoldTests(unittest.TestCase):
    def test_one_open_per_key_then_close_on_clean_run(self):
        st: dict = {}
        fail = [_rec(level="START"),
                _rec(level="ERROR", key="prune_api_cache:error-api_cache-still"),
                _rec(level="EXIT", exit=1, key="exit:prune_api_cache",
                     run_keys=["exit:prune_api_cache", "prune_api_cache:error-api_cache-still"])]
        a1 = bi.fold(st, fail, now=1000)
        self.assertEqual([a[0] for a in a1], ["open", "open"])
        a2 = bi.fold(st, fail, now=2000)  # the next hourly failure: counted, no twin, no spam
        self.assertEqual(a2, [])
        self.assertEqual(st["exit:prune_api_cache"]["count"], 2)
        ok = [_rec(level="START"), _rec(level="EXIT", exit=0, msg="EXIT 0", run_keys=[],
                                        ts="2026-09-29T11:06:50Z")]  # clean, 6h on
        a3 = bi.fold(st, ok, now=3000)
        self.assertEqual(sorted(a[1] for a in a3 if a[0] == "close"),
                         ["exit:prune_api_cache", "prune_api_cache:error-api_cache-still"])
        self.assertEqual(st, {})

    def test_flapping_job_stays_one_open_item(self):
        """Live 2026-09-29: search_filter_latency_canary failed every other 5-min run and the
        first cut filed+closed 3 issues in one pass. A clean run < CLEAN_S after the last
        failure must not close; the next failure is a repeat, not a new item."""
        st: dict = {}
        acts = []
        for i, rc in enumerate([1, 0, 1, 0, 1]):
            ts = f"2026-09-29T13:{i * 5:02d}:00Z"
            recs = [_rec(job="c", level="EXIT", exit=rc, ts=ts, key="exit:c" if rc else None,
                         run_keys=["exit:c"] if rc else [])]
            acts += bi.fold(st, recs, now=0)
        self.assertEqual([a[0] for a in acts], ["open"])
        clean = [_rec(job="c", level="EXIT", exit=0, ts="2026-09-29T19:20:00Z", run_keys=[])]
        self.assertEqual([a[0] for a in bi.fold(st, clean, now=0)], ["close"])

    def test_hourly_flapper_stays_one_item(self):
        """Live 2026-10-01: a canary failing once an hour or two, clean in between, got a new
        issue each time (21 in 48h). Clean for 1-5h is still the same incident."""
        st: dict = {}
        acts = []
        for i, rc in enumerate([1, 0, 0, 1, 0, 1]):
            ts = f"2026-09-29T{10 + i:02d}:00:00Z"
            recs = [_rec(job="c", level="EXIT", exit=rc, ts=ts, key="exit:c" if rc else None,
                         run_keys=["exit:c"] if rc else [])]
            acts += bi.fold(st, recs, now=0)
        self.assertEqual([a[0] for a in acts], ["open"])

    def test_alert_still_in_run_keys_stays_open_and_comments_after_6h(self):
        st: dict = {}
        run = [_rec(job="pg_health", level="ALERT", key="pg_health:pooltimeouts"),
               _rec(job="pg_health", level="EXIT", exit=0, run_keys=["pg_health:pooltimeouts"])]
        self.assertEqual([a[0] for a in bi.fold(st, run, now=0)], ["open"])
        self.assertEqual(bi.fold(st, run, now=300), [])
        self.assertEqual([a[0] for a in bi.fold(st, run, now=6 * 3600 + 1)], ["repeat"])

    def test_other_jobs_exit_does_not_close_and_unframed_quiet_closes(self):
        st: dict = {}
        bi.fold(st, [_rec(job="nginx:error", level="ERROR", key="nginx:error:upstream")], now=0)
        self.assertEqual(bi.fold(st, [_rec(job="pg_health", level="EXIT", exit=0, run_keys=[])], now=10), [])
        later = bi._t(_rec(), 0) + 24 * 3600 + 1  # 24h after the box's own timestamp
        self.assertEqual([a[0] for a in bi.fold(st, [], now=later)], ["close"])

    def test_info_and_keyless_lines_never_file(self):
        st: dict = {}
        self.assertEqual(bi.fold(st, [_rec(level="INFO"), _rec(level="ERROR"),
                                      _rec(level="EXIT", exit=0, run_keys=[])], now=0), [])


class ApplyTests(unittest.TestCase):
    def test_open_then_close_in_one_batch_closes_the_issue_it_filed(self):
        st: dict = {}
        recs = [_rec(level="EXIT", exit=1, key="exit:j", job="j", run_keys=["exit:j"]),
                _rec(level="EXIT", exit=0, job="j", run_keys=[], ts="2026-09-29T11:06:50Z")]
        actions = bi.fold(st, recs, now=0)
        calls = []
        with mock.patch("inbox.file_or_comment_alert",
                        return_value=("https://github.com/o/r/issues/77", True)), \
             mock.patch("inbox.repo_slug", return_value="o/r"):
            out = bi.apply(actions, st, {}, run=lambda c: calls.append(c))
        self.assertEqual(out[0], "filed exit:j -> https://github.com/o/r/issues/77")
        self.assertEqual(calls[0][:5], ["gh", "issue", "close", "--repo", "o/r"])
        self.assertEqual(calls[0][5], "77")


if __name__ == "__main__":
    unittest.main()
