"""Nothing waits on a human (Reif, 2026-09-28): "zero should ever wait on a human" and, on the
asks, "if it's an opinion, make the opinion yourself."

THE GAP: 9 fleet:reif-priority issues sat on fleet:needs-human-op and 4 on fleet:dead-end-blocked,
and 15 asks sat open, most of them opinions (decision/acceptance/idea/infra) the fleet could make.
Now: `ask.py file` with an opinion class files a notice and the member proceeds; only the
one-way-door classes (credential, money) open a question. The member specs say the same, and
marie un-parks what is parked on something that is not a one-way door.

Run: python3 scripts/test_zero_human_waits.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import ask  # noqa: E402
import fleet_db  # noqa: E402


def _file(d, cls):
    rc = ask.main(["--db-path", str(Path(d) / "fleet.db"),
                   "--authority-path", str(Path(d) / "none" / "authority.json"),
                   "file", "--member", "marie", "--why", f"a {cls} question", "--class", cls,
                   "--no-notify"])
    assert rc == 0
    conn = fleet_db.connect(Path(d) / "fleet.db")
    return ask.list_asks(conn, status="all")[0]["status"]


class OpinionsAreDecidedNotAsked(unittest.TestCase):
    def test_opinion_classes_file_a_notice_with_no_grant(self):
        for cls in ("decision", "acceptance", "idea", "infra", "product-copy", "pricing",
                    "external-merge"):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(_file(d, cls), "notice", cls)

    def test_one_way_doors_still_ask(self):
        for cls in ("credential", "money"):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(_file(d, cls), "open", cls)


class OnlyDumbledoreReachesReif(unittest.TestCase):
    """Reif, 2026-09-28: "asks only from dumbledore" and "dumbledore actually denies the request
    because another way is found." Any other member's ask messages dumbledore (urgent, it wakes
    him) and never pages Reif. dumbledore denies with a path by default; only an escalation,
    with a reason, emails Reif under dumbledore's name, and Reif's email reply still answers it."""

    def setUp(self):
        import os, subprocess
        from unittest import mock
        import fleet_msg
        self.d = tempfile.mkdtemp()
        self.db = str(Path(self.d) / "fleet.db")
        self.pages, self.woken = [], []
        real = subprocess.run
        def fake(cmd, *a, **kw):
            if cmd and cmd[0] == "bash" and str(cmd[1]).endswith("fleet_alert.sh"):
                self.pages.append((cmd, kw.get("env", {})))
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            return real(cmd, *a, **kw)
        for p in (mock.patch.object(ask.subprocess, "run", fake),
                  mock.patch.object(ask, "issue_for_ask", lambda *a, **k: ""),
                  mock.patch.object(ask, "settle_ask_issue", lambda *a, **k: ""),
                  mock.patch.object(fleet_msg, "_default_launcher",
                                    lambda m, r: self.woken.append(m)),
                  mock.patch.dict(os.environ, {"FLEET_LOG_DIR": self.d})):
            p.start(); self.addCleanup(p.stop)

    def ask(self, *argv):
        return ask.main(["--db-path", self.db, "--authority-path",
                         str(Path(self.d) / "none" / "authority.json"), *argv])

    def conn(self):
        return fleet_db.connect(Path(self.db))

    def file(self, member="gru", cls="credential"):
        self.assertEqual(self.ask("file", "--member", member, "--why", "rotate the leaked token?",
                                  "--class", cls), 0)
        return ask.list_asks(self.conn(), status="all")[0]["id"]

    def test_member_ask_messages_dumbledore_and_never_pages_reif(self):
        import fleet_msg
        ask_id = self.file()
        self.assertEqual(self.pages, [])
        box = fleet_msg.inbox(self.conn(), "dumbledore")
        self.assertEqual(len(box), 1)
        self.assertEqual((box[0]["kind"], box[0]["sender"], box[0]["ask_id"]), ("ask", "gru", ask_id))
        self.assertEqual(self.woken, ["dumbledore"])

    def test_credential_ask_is_denied_with_a_path_not_a_page(self):
        import fleet_msg
        ask_id = self.file()
        self.assertEqual(self.ask("deny", str(ask_id), "--me", "dumbledore", "--path",
                                  "the token is already in fleet.env; the-fixer wires it",
                                  "--to", "the-fixer"), 0)
        self.assertEqual(self.pages, [])
        row = ask.list_asks(self.conn(), status="all")[0]
        self.assertEqual((row["status"], row["answered_by"]), ("denied", "dumbledore"))
        self.assertIn("fleet.env", row["answer"])
        self.assertEqual(fleet_msg.inbox(self.conn(), "dumbledore"), [])
        work = fleet_msg.inbox(self.conn(), "the-fixer")
        self.assertEqual(len(work), 1)
        self.assertIn(f"ask #{ask_id}", work[0]["body"])
        self.assertEqual(len(ask.list_asks(self.conn(), status="all")), 1, "one ask id end to end")

    def test_escalation_pages_reif_as_dumbledore_and_email_reply_answers(self):
        ask_id = self.file()
        self.assertNotEqual(self.ask("escalate", str(ask_id), "--me", "gru", "--reason", "x"), 0)
        self.assertEqual(self.pages, [])
        self.assertEqual(self.ask("escalate", str(ask_id), "--me", "dumbledore", "--reason",
                                  "the token must be rotated at the provider; only Reif's login can"), 0)
        self.assertEqual(len(self.pages), 1)
        cmd, env = self.pages[0]
        self.assertEqual(env.get("FLEET_ALERT_EMAIL_LEG"), "1")
        # Reif, 2026-09-28: "label these as officially from dumbledore" -- every escalation
        # email is branded so he can trust the sender at a glance: subject prefix + From name.
        self.assertIn(f"[dumbledore] fleet ask #{ask_id} from dumbledore (for gru)", cmd)
        self.assertEqual(env.get("MAIL_FROM"), "Dumbledore (fleet) <hello@philanthropy.org>")
        self.assertIn(f"yes {ask_id}", cmd[-1])
        self.assertEqual(len(ask.list_asks(self.conn(), status="all")), 1, "one ask id end to end")
        # Reif's reply, the way webhook_receiver/inbox.py applies it.
        import inbox
        parsed = inbox.parse_reply(f"yes {ask_id}: rotated")
        rc = [self.ask("answer", str(a["ask_id"]), "--answer", a["answer"],
                       "--answered-by", "reif (email)") for a in parsed["answers"]]
        self.assertEqual(rc, [0])
        row = ask.list_asks(self.conn(), status="all")[0]
        self.assertEqual((row["status"], row["answered_by"]), ("answered", "reif (email)"))
        self.assertEqual(ask.triage_rate(self.conn())["escalated"], 1)

    def test_escalation_email_leads_plain_and_lists_every_other_open_ask(self):
        # Reif, 2026-09-29: "if it has a new one - append it to all that have not been
        # resolved" and "write in plain simple English". Each escalation email opens with
        # dumbledore's plain reason, then lists every OTHER escalated ask still unanswered.
        first, second, answered = self.file(), self.file(), self.file()
        for i, why in ((first, "Prod box needs a GitHub read token"),
                       (answered, "already handled"),
                       (second, "Resize the database disk")):
            self.assertEqual(self.ask("escalate", str(i), "--me", "dumbledore", "--reason", why), 0)
            if i == answered:
                self.assertEqual(self.ask("answer", str(i), "--answer", "done",
                                          "--answered-by", "reif (email)"), 0)
        body = self.pages[-1][0][-1]
        self.assertTrue(body.startswith("Resize the database disk"), body)
        self.assertIn(f"Also still waiting on you:\n- #{first}: Prod box needs a GitHub read token", body)
        self.assertNotIn(f"#{answered}:", body, "an answered ask is not listed")
        self.assertNotIn(f"- #{second}:", body, "the new ask is not listed twice")
        self.assertIn(f"yes {second}", body)
        # With nothing else open, no list.
        self.assertNotIn("Also still waiting", self.pages[0][0][-1])

    def test_escalation_rate_counts_escalated_over_triaged(self):
        for _ in range(3):
            i = self.file()
            self.ask("deny", str(i), "--me", "dumbledore", "--path", "another way")
        i = self.file()
        self.ask("escalate", str(i), "--me", "dumbledore", "--reason", "only Reif's login")
        r = ask.triage_rate(self.conn())
        self.assertEqual((r["triaged"], r["escalated"], r["denied"]), (4, 1, 3))

    def test_dumbledores_own_ask_emails_reif(self):
        self.file(member="dumbledore")
        self.assertEqual(len(self.pages), 1)
        self.assertEqual(self.pages[0][1].get("FLEET_ALERT_EMAIL_LEG"), "1")

    def test_opinion_never_pages_or_messages(self):
        import fleet_msg
        self.file(cls="decision")
        self.assertEqual(self.pages, [])
        self.assertEqual(fleet_msg.inbox(self.conn(), "dumbledore"), [])


class SpecsSayNothingWaitsOnAHuman(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text()

    def test_specs_route_asks_through_dumbledore(self):
        self.assertIn("Every ask goes to dumbledore", self.read("agents/persona_law.md"))
        d = self.read("members/dumbledore/dumbledore.md")
        for s in ("ask.py deny", "ask.py escalate", "ask.py triage-rate", "Escalation-rate:"):
            self.assertIn(s, d)

    def test_persona_law_has_the_rule(self):
        law = self.read("agents/persona_law.md")
        self.assertIn("## 2b. Nothing waits on a human", law)
        self.assertIn("--class credential|money", law)

    def test_asking_is_way_rare_and_fleet_owners_may_hold_a_park(self):
        law = self.read("agents/persona_law.md")
        self.assertIn("Asking Reif is way rare", law)
        self.assertIn("Parking an item on a fleet owner (the-fixer, marie) is fine; on Reif, never.", law)
        fixer = self.read("members/the-fixer/the-fixer.md")
        self.assertIn("a `stalled-item` one", fixer)
        marie = self.read("members/marie/marie.md")
        self.assertIn("parked on a fleet owner, not a human", marie)

    def test_minion_blocked_line_is_only_a_one_way_door(self):
        minion = self.read("members/minion/minion.md")
        self.assertNotIn("an open question only Reif can answer", minion)
        self.assertIn("one-way door", minion)

    def test_marie_unparks_and_decides_scope(self):
        marie = self.read("members/marie/marie.md")
        self.assertIn("## Part A0 — un-park", marie)
        self.assertNotIn("always waits for Reif himself", marie)

    def test_gru_honors_unparked_and_answers_every_non_door_ask(self):
        gru = self.read("members/gru/gru.md")
        self.assertIn("`Un-parked:` comment", gru)
        self.assertIn("every class except `credential` and `money`", gru)


if __name__ == "__main__":
    unittest.main()
