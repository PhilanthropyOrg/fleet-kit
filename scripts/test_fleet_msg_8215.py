"""philanthropy#8215 spec amendment: member-to-member messages (fleet_msg.py).

Given gru drops more than 3 items at the quality gate, When its pass ends, Then marie and jefe
each have an unread message listing them, and marie's next pass sees it before anything else.
Given a message goes unacked for 2 cadences, When the watchdog runs, Then it escalates to jefe,
then to Reif (one ask).
Given the fleet console, Then a tile shows open messages by member and the oldest unacked one.

Run: python3 scripts/test_fleet_msg_8215.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402
import fleet_msg  # noqa: E402
import gate_drops  # noqa: E402
import journey_issue_filer  # noqa: E402

H = 3600.0


def _item(n, labels=("fleet:priority-high",), body="Vision-link: none (maintenance)\n\nGiven a user, When they act, Then it works"):
    return {"number": n, "labels": [{"name": x} for x in labels], "body": body, "comments": []}


class Ok:
    returncode, stdout, stderr = 0, "", ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "fleet.db"
        self.conn = fleet_db.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class GateDropMessage(Base):
    def _run(self, n_unspecced):
        # No Given/When/Then: a missing quality label is fixed mechanically since philanthropy#8218.
        items = [_item(100 + i, labels=("fleet:priority-high", "quality:solid"),
                       body="Vision-link: none (maintenance)") for i in range(n_unspecced)]
        items.append(_item(900, labels=("fleet:priority-high", "quality:solid")))
        p = gate_drops.plan(items, "gru-test")
        old = gate_drops.DROP_LOG
        gate_drops.DROP_LOG = Path(self.tmp.name) / "gate_drops.jsonl"
        try:
            res = gate_drops.apply(p, None, "gru-test", run=lambda cmd: Ok(), db_path=str(self.db))
        finally:
            gate_drops.DROP_LOG = old
        return p, res

    def test_more_than_three_drops_messages_marie_and_jefe_listing_them(self):
        p, res = self._run(4)
        self.assertEqual(p["eligible"], [900])
        self.assertEqual(sorted(s["to"] for s in res["sent"]), ["jefe", "marie"])
        for who in ("marie", "jefe"):
            box = fleet_msg.inbox(self.conn, who)
            self.assertEqual(len(box), 1, who)
            self.assertEqual(box[0]["kind"], "gate-drop")
            self.assertEqual(box[0]["items"], [100, 101, 102, 103])
            self.assertIn("4 of 5 candidates", box[0]["body"])
            self.assertIn("acceptance (4): #100, #101, #102, #103", box[0]["body"])

    def test_three_or_fewer_drops_send_nothing(self):
        p, res = self._run(3)
        self.assertIsNone(p["message"])
        self.assertIsNone(res["sent"])
        self.assertEqual(fleet_msg.inbox(self.conn, "marie"), [])

    def test_second_pass_within_6h_is_deduped(self):
        self._run(5)
        _, res = self._run(6)
        self.assertTrue(all(s["deduped"] for s in res["sent"]))
        self.assertEqual(len(fleet_msg.inbox(self.conn, "marie")), 1)


class InboxAckReply(Base):
    def test_render_puts_ack_and_reply_commands_in_front_of_the_member(self):
        fleet_msg.send(self.conn, "gru", ["marie"], "gate-drop", "k", "fix these", [7, 8])
        text = fleet_msg.render(fleet_msg.inbox(self.conn, "marie"), "marie")
        self.assertTrue(text.startswith("## INBOX -- 1 unread message(s) for marie"))
        self.assertIn("fleet_msg.py ack --me marie --id", text)
        self.assertIn("fleet_msg.py reply --me marie --id", text)
        self.assertIn("items: #7, #8", text)
        self.assertEqual(fleet_msg.render([], "marie"), "")

    def test_ack_and_reply_close_with_the_note_only_for_the_recipient(self):
        (a,) = fleet_msg.send(self.conn, "gru", ["marie"], "x", "1", "b")
        (b,) = fleet_msg.send(self.conn, "gru", ["marie"], "x", "2", "b")
        with self.assertRaises(ValueError):
            fleet_msg.close(self.conn, "jefe", a["id"], "not mine")
        with self.assertRaises(ValueError):
            fleet_msg.close(self.conn, "marie", a["id"], "  ")
        self.assertEqual(fleet_msg.close(self.conn, "marie", a["id"], "did it")["status"], "acked")
        r = fleet_msg.close(self.conn, "marie", b["id"], "epic, nothing to do", acted=False)
        self.assertEqual((r["status"], r["ack_note"]), ("replied", "epic, nothing to do"))
        self.assertEqual(fleet_msg.inbox(self.conn, "marie"), [])

    def test_pregate_is_green_only_on_an_empty_inbox(self):
        out = lambda: subprocess.run(  # noqa: E731
            [sys.executable, str(HERE / "fleet_msg.py"), "--db-path", str(self.db),
             "pregate", "--me", "jefe"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out(), "green (inbox empty)")
        fleet_msg.send(self.conn, "gru", ["jefe"], "x", "k", "b")
        self.assertEqual(out(), "FIRE 1 unread message(s)")


class Watchdog(Base):
    def test_unacked_for_two_cadences_goes_to_jefe_then_one_ask_for_reif(self):
        t0 = 1_000_000.0
        cad = lambda m: {"marie": 4 * H}.get(m, H)  # noqa: E731
        (m,) = fleet_msg.send(self.conn, "gru", ["marie"], "gate-drop", "k", "fix", [1], now=t0)
        # 7h: under 2 of marie's 4h cadences -> nothing
        self.assertEqual(fleet_msg.watchdog(self.conn, t0 + 7 * H, cad)["escalated_to_jefe"], [])
        r = fleet_msg.watchdog(self.conn, t0 + 8.1 * H, cad)
        self.assertEqual(r["escalated_to_jefe"], [m["id"]])
        (esc,) = fleet_msg.inbox(self.conn, "jefe")
        self.assertEqual((esc["kind"], esc["parent_id"]), ("escalation", m["id"]))
        # re-running does not re-escalate the same message
        self.assertEqual(fleet_msg.watchdog(self.conn, t0 + 8.2 * H, cad)["escalated_to_jefe"], [])
        # jefe sits on it for 2 of its (1h) cadences -> ONE ask, even with a second one piling up
        (m2,) = fleet_msg.send(self.conn, "sentry", ["jefe"], "permission-denial", "k2", "b",
                               now=t0 + 8.1 * H)
        filed = []
        fa = lambda **kw: filed.append(kw) or 42  # noqa: E731
        r = fleet_msg.watchdog(self.conn, t0 + 10.3 * H, cad, file_ask=fa)
        self.assertEqual(sorted(r["escalated_to_reif"]), sorted([esc["id"], m2["id"]]))
        self.assertEqual((r["ask_id"], len(filed)), (42, 1))
        self.assertTrue(filed[0]["why"].startswith(fleet_msg.REIF_TAG))
        self.assertEqual(fleet_msg.watchdog(self.conn, t0 + 12 * H, cad, file_ask=fa)["ask_id"], None)
        self.assertEqual(len(filed), 1)

    def test_jefe_acking_the_escalation_closes_the_original(self):
        t0 = 1_000_000.0
        (m,) = fleet_msg.send(self.conn, "gru", ["marie"], "gate-drop", "k", "fix", now=t0)
        fleet_msg.watchdog(self.conn, t0 + 3 * H, lambda _: H)
        (esc,) = fleet_msg.inbox(self.conn, "jefe")
        fleet_msg.close(self.conn, "jefe", esc["id"], "labeled them myself")
        orig = fleet_msg.get(self.conn, m["id"])
        self.assertEqual(orig["status"], "acked")
        self.assertIn("labeled them myself", orig["ack_note"])

    def test_cadence_reads_the_instance_knob_then_the_spec(self):
        specs = {"sentry": {"schedule": {"interval_s": 10800}}, "gru": {"schedule": {"hourly_at_minute": 3}}}
        self.assertEqual(fleet_msg.cadence_s("marie", specs, {"FLEET_MARIE_CADENCE": "*/4"}), 4 * H)
        self.assertEqual(fleet_msg.cadence_s("gru", specs, {"FLEET_GRU_CADENCE": "*"}), H)
        self.assertEqual(fleet_msg.cadence_s("sentry", specs, {}), 3 * H)
        self.assertEqual(fleet_msg.cadence_s("nobody", specs, {}), H)


class Console(Base):
    def test_summary_has_open_by_member_and_the_oldest(self):
        fleet_msg.send(self.conn, "gru", ["marie", "jefe"], "gate-drop", "k", "b", now=100.0)
        fleet_msg.send(self.conn, "sentry", ["the-fixer"], "journey-failing", "j", "b", now=200.0)
        s = fleet_msg.summary(self.conn, now=3700.0)
        self.assertEqual(s["open"], 3)
        self.assertEqual(s["by_member"], {"jefe": 1, "marie": 1, "the-fixer": 1})
        self.assertEqual((s["oldest"]["kind"], s["oldest"]["age_s"]), ("gate-drop", 3600))

    def test_tile_is_registered(self):
        ids = [m["id"] for m in json.loads((HERE / "metrics.json").read_text())["metrics"]]
        self.assertIn("fleet.msgs_open", ids)


class OtherSenders(Base):
    def test_idle_prs_go_to_the_owning_member(self):
        members = {"gru", "marie", "the-fixer", "jefe"}
        prs = [
            {"number": 1, "headRefName": "member/minion-item8196", "updatedAt": "1970-01-01T00:00:00Z"},
            {"number": 2, "headRefName": "member/the-fixer-99-1", "updatedAt": "1970-01-01T00:00:00Z"},
            {"number": 3, "headRefName": "hq/8215-x", "updatedAt": "1970-01-01T00:00:00Z"},
            {"number": 4, "headRefName": "member/minion-item1", "updatedAt": "1970-01-01T05:00:00Z"},
            {"number": 5, "headRefName": "member/minion-item2", "updatedAt": "1970-01-01T00:00:00Z", "isDraft": True},
        ]
        msgs = fleet_msg.idle_pr_messages(prs, members, now=6 * H)
        self.assertEqual([(m["to"], m["items"]) for m in msgs], [("gru", [1]), ("the-fixer", [2])])

    def test_a_failing_journey_messages_the_fixer(self):
        sent = []
        journey_issue_filer.message_fixer(
            {"filed": [{"issue": 12}], "commented": [{"issue": 9}]},
            send=lambda *a: sent.append(a) or [])
        (sender, to, kind, key, body, items), = sent
        self.assertEqual((sender, to, kind, key, items), ("sentry", ["the-fixer"], "journey-failing", "journeys:9,12", [9, 12]))
        self.assertIsNone(journey_issue_filer.message_fixer({"filed": [], "commented": []}, send=lambda *a: 1/0))


class Wiring(unittest.TestCase):
    def test_run_member_puts_the_inbox_first(self):
        sh = (HERE / "run_member.sh").read_text()
        i = sh.index("fleet_msg.py\" inbox --me \"$MEMBER\" --render")
        self.assertGreater(i, sh.index('NORTH_FILE="$LOG_DIR/NORTH.md"'),
                           "the inbox must be prepended after NORTH/HANDOFF so it is read first")
        self.assertLess(i, sh.index("REPORT_CONTRACT=$("))

    def test_jefe_is_a_pregated_scheduled_member(self):
        spec = json.loads((HERE.parent / "members/jefe/jefe.fleet.json").read_text())
        self.assertTrue(spec["enabled"])
        self.assertEqual(spec["llm"]["pregate"], "members/jefe/pregate.sh")
        ep = (HERE.parent / "entrypoint.sh").read_text()
        self.assertIn("run_member.sh jefe", ep)
        self.assertIn("fleet_msg.py watchdog", ep)

    def test_marie_handles_her_inbox_before_part_a(self):
        md = (HERE.parent / "members/marie/marie.md").read_text()
        self.assertLess(md.index("## Inbox"), md.index("## Part A"))
        self.assertIn("0. Part Inbox", md)


if __name__ == "__main__":
    os.environ.setdefault("FLEET_LOG_DIR", tempfile.mkdtemp())
    unittest.main(verbosity=2)
