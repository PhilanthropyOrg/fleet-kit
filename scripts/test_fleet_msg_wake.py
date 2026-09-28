"""Urgent fleet_msg.py sends wake the recipient now instead of waiting for its next cron pass.

THE GAP: cron cadence was the only thing that read a message before this -- marie runs every 4h
and had 7 unread, nerd sat on one for 7h, dumbledore's `cause` msg landed 30min after his pass
had already ended for that cadence. `send --kind cause` (and incident/pr-block/nudge) must now
launch the recipient's next pass immediately, detached, coalesced to one per cooldown, skipping
hq / unspecced / disabled recipients. `gate-drop` (not in WAKE_KINDS) must NOT wake.

Run: python3 scripts/test_fleet_msg_wake.py
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402
import fleet_msg  # noqa: E402


SPECS = {
    "marie": {"name": "marie", "enabled": True},
    "gru": {"name": "gru", "enabled": True},
    "dumbledore": {"name": "dumbledore", "enabled": False},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "fleet.db"
        self.conn = fleet_db.connect(self.db)
        self.log_dir = Path(self.tmp.name) / "logs"
        self.launched = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _launcher(self, member, reason):
        self.launched.append((member, reason))

    def _send(self, sender, to, kind, key, body="urgent", now=None):
        sent = fleet_msg.send(self.conn, sender, to, kind, key, body, now=now)
        return fleet_msg.wake(self.conn, sent, kind, specs=SPECS, now=now,
                              launcher=self._launcher, log_dir=self.log_dir)


class CauseWakesTheRecipient(Base):
    def test_cause_msg_wakes_recipient_once(self):
        out = self._send("dumbledore", "marie", "cause", "k1", now=1000.0)
        self.assertEqual(self.launched, [("marie", "msg-1-cause")])
        self.assertTrue(out[0]["woken"])
        self.assertIsNone(out[0]["wake_reason"])

    def test_second_wake_within_cooldown_is_skipped(self):
        self._send("dumbledore", "marie", "cause", "k1", now=1000.0)
        out = self._send("dumbledore", "marie", "incident", "k2", now=1000.0 + 60)
        self.assertEqual(self.launched, [("marie", "msg-1-cause")])  # no second launch
        self.assertFalse(out[0]["woken"])
        self.assertEqual(out[0]["wake_reason"], "cooldown")

    def test_wake_after_cooldown_elapses_fires_again(self):
        self._send("dumbledore", "marie", "cause", "k1", now=1000.0)
        out = self._send("dumbledore", "marie", "incident", "k2",
                         now=1000.0 + fleet_msg.WAKE_COOLDOWN_S + 1)
        self.assertEqual(len(self.launched), 2)
        self.assertTrue(out[0]["woken"])


class GateDropDoesNotWake(Base):
    def test_gate_drop_kind_is_not_a_wake_kind(self):
        out = self._send("gru", "marie", "gate-drop", "gate-drops", now=1000.0)
        self.assertEqual(self.launched, [])
        self.assertFalse(out[0]["woken"])
        self.assertEqual(out[0]["wake_reason"], "kind")


class DisabledMemberNotWoken(Base):
    def test_disabled_recipient_is_skipped(self):
        out = self._send("gru", "dumbledore", "cause", "k1", now=1000.0)
        self.assertEqual(self.launched, [])
        self.assertEqual(out[0]["wake_reason"], "disabled")


class HqNotWoken(Base):
    def test_hq_is_skipped(self):
        out = self._send("gru", "hq", "cause", "k1", now=1000.0)
        self.assertEqual(self.launched, [])
        self.assertEqual(out[0]["wake_reason"], "hq")


class UnspeccedMemberNotWoken(Base):
    def test_recipient_with_no_spec_is_skipped(self):
        out = self._send("gru", "ghost", "cause", "k1", now=1000.0)
        self.assertEqual(self.launched, [])
        self.assertEqual(out[0]["wake_reason"], "no-spec")


class DedupedRowNotWoken(Base):
    def test_a_deduped_row_is_not_launched_again(self):
        self._send("gru", "marie", "cause", "same-key", now=1000.0)
        # second send with the same (to, kind, key) inside DEDUPE_S is deduped by send() itself
        out = self._send("gru", "marie", "cause", "same-key", now=1000.0 + 60)
        self.assertTrue(out[0]["deduped"])
        self.assertEqual(out[0]["wake_reason"], "deduped")
        self.assertEqual(len(self.launched), 1)  # only the first send woke marie


class DefaultLauncherIsInjectable(Base):
    def test_default_launcher_reads_FLEET_RUN_MEMBER_and_launches_detached(self):
        """dispatch_member.sh's own env-override name -- production wires the real launcher;
        this checks it resolves the injected script path and shape without spawning a real
        pass (Popen itself is stubbed so the test doesn't depend on `setsid` existing here)."""
        import os
        from unittest import mock

        stub = Path(self.tmp.name) / "run_member.sh"
        calls = []

        class FakePopen:
            def __init__(self, cmd, **kw):
                calls.append((cmd, kw))

        old = os.environ.get("FLEET_RUN_MEMBER")
        os.environ["FLEET_RUN_MEMBER"] = str(stub)
        try:
            with mock.patch.object(fleet_msg, "_log_dir", return_value=self.log_dir), \
                 mock.patch.object(fleet_msg.subprocess, "Popen", FakePopen):
                sent = fleet_msg.send(self.conn, "gru", "marie", "cause", "k1", "urgent", now=1000.0)
                fleet_msg.wake(self.conn, sent, "cause", specs=SPECS, now=1000.0)
        finally:
            if old is None:
                os.environ.pop("FLEET_RUN_MEMBER", None)
            else:
                os.environ["FLEET_RUN_MEMBER"] = old
        self.assertEqual(len(calls), 1)
        cmd, kw = calls[0]
        self.assertEqual(cmd, ["setsid", "nohup", "bash", str(stub), "marie"])
        self.assertEqual(kw["stdin"], fleet_msg.subprocess.DEVNULL)
        self.assertEqual(kw["env"]["FLEET_FIRED_BY"], "fleet_msg")
        self.assertEqual(kw["env"]["FLEET_FIRED_REASON"], "msg-1-cause")


if __name__ == "__main__":
    unittest.main()
