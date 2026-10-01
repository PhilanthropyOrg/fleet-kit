"""The growth member's mail tool refuses everything the founder's policy does not allow.

Mailing people is a one-way door, so every refusal here is a test: policy off, any cap unset,
a lead with no source or basis, a source or region the founder never listed, a suppressed
address (any letter case, checked at send time), a cold send from the product's own domain,
two sends racing for the last slot. Real sends go to a fake provider on 127.0.0.1 -- no test
in this file can reach a real mail service.

Run: python3 scripts/test_outreach_send.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))


class FakeProvider(BaseHTTPRequestHandler):
    seen: list = []
    status = 200
    on_request = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        FakeProvider.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                  "ua": self.headers.get("User-Agent"), "body": body})
        if FakeProvider.on_request:
            FakeProvider.on_request(body)
        self.send_response(FakeProvider.status)
        self.end_headers()
        self.wfile.write(b"{}")


_provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
threading.Thread(target=_provider.serve_forever, daemon=True).start()
FAKE = f"http://127.0.0.1:{_provider.server_address[1]}"
os.environ["RESEND_API_URL_BASE"] = FAKE   # before inbox is imported, so it can only ever post here

import inbox  # noqa: E402
import outreach_send as o  # noqa: E402
import webhook_receiver as wr  # noqa: E402

assert inbox.RESEND_API == FAKE, "tests must never be able to reach a real mail provider"

SECRET = "s" * 32
LEAD = {"email": "ann@example.org", "source": "own_signup", "basis": "opt_in"}
COLD = {"email": "bob@example.org", "source": "partner_list", "basis": "legitimate_interest", "region": "US"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        key = Path(self.tmp.name) / "key"
        key.write_text("re_test_key\n")
        # A complete lifecycle policy. Each refusal test removes or changes ONE thing.
        self.policy = {
            "FLEET_LOG_DIR": self.tmp.name,
            "FLEET_OUTREACH_ENABLED": "1",
            "FLEET_OUTREACH_DAILY_CAP": "10",
            "FLEET_OUTREACH_PER_RECIPIENT_CAP": "1",
            "FLEET_OUTREACH_FROM": "Acme <hi@mail.acme-mail.example>",
            "FLEET_OUTREACH_POSTAL_ADDRESS": "1 Main St, Springfield",
            "FLEET_OUTREACH_UNSUB_URL": "https://fleet.example/webhook/unsubscribe",
            "FLEET_OUTREACH_UNSUB_SECRET": SECRET,
            "FLEET_OUTREACH_KEY_FILE": str(key),
        }
        self.cold = {"FLEET_OUTREACH_COLD_ENABLED": "1", "FLEET_OUTREACH_COLD_REGIONS": "US",
                     "FLEET_OUTREACH_PRODUCT_DOMAIN": "acme.example",
                     "FLEET_OUTREACH_SOURCES": "own_signup partner_list"}
        FakeProvider.seen = []
        FakeProvider.status = 200
        FakeProvider.on_request = None
        self.env()

    def env(self, drop=(), **over):
        """Install the policy as the process environment, minus `drop`, plus `over`."""
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("FLEET_OUTREACH_") and k not in ("FLEET_REPLY_TO", "FLEET_ENV_FILE")}
        clean.update({k: v for k, v in {**self.policy, **over}.items() if k not in drop})
        p = mock.patch.dict(os.environ, clean, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def send(self, leads=None, mail_type="lifecycle", really=True):
        return o.send(mail_type, "Hello", "Body text", leads or [dict(LEAD)], "t", really=really)

    def assertRefused(self, needle, **kw):
        with self.assertRaises(o.Refused) as cm:
            self.send(**kw)
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(FakeProvider.seen, [], "a refused run must not reach the provider")


class PolicyRefusals(Base):
    def test_nothing_set_means_nothing_sends(self):
        self.env(drop=set(self.policy) - {"FLEET_LOG_DIR"})
        self.assertRefused("outreach is off")
        self.assertRefused("outreach is off", really=False)

    def test_enabled_must_be_exactly_1(self):
        for v in ("0", "true", "yes", ""):
            self.env(FLEET_OUTREACH_ENABLED=v)
            self.assertRefused("outreach is off")

    def test_unset_or_bad_daily_cap_is_zero_and_refuses(self):
        self.env(drop={"FLEET_OUTREACH_DAILY_CAP"})
        self.assertRefused("no daily cap")
        for v in ("0", "-5", "lots", ""):
            self.env(FLEET_OUTREACH_DAILY_CAP=v)
            self.assertRefused("no daily cap")

    def test_unset_per_recipient_cap_refuses(self):
        self.env(drop={"FLEET_OUTREACH_PER_RECIPIENT_CAP"})
        self.assertRefused("no per-person cap")

    def test_no_sender_refuses(self):
        self.env(drop={"FLEET_OUTREACH_FROM"})
        self.assertRefused("no sender address")

    def test_no_postal_address_refuses(self):
        self.env(drop={"FLEET_OUTREACH_POSTAL_ADDRESS"})
        self.assertRefused("no postal address")

    def test_no_or_plain_http_unsubscribe_link_refuses(self):
        self.env(drop={"FLEET_OUTREACH_UNSUB_URL"})
        self.assertRefused("unsubscribe link")
        self.env(FLEET_OUTREACH_UNSUB_URL="http://fleet.example/webhook/unsubscribe")
        self.assertRefused("unsubscribe link")

    def test_short_or_missing_unsubscribe_secret_refuses(self):
        self.env(FLEET_OUTREACH_UNSUB_SECRET="short")
        self.assertRefused("unsubscribe secret")

    def test_key_comes_from_a_file_never_the_environment(self):
        self.env(drop={"FLEET_OUTREACH_KEY_FILE"}, RESEND_API_KEY="re_live_product_key")
        self.assertRefused("no provider key")

    def test_subject_cannot_smuggle_headers(self):
        with self.assertRaises(o.Refused):
            o.send("lifecycle", "Hi\nBcc: x@y.example", "Body", [dict(LEAD)], really=True)

    def test_unknown_type_refuses(self):
        self.assertRefused("type must be", mail_type="announcement")


class LeadRefusals(Base):
    def refused(self, lead, mail_type="lifecycle"):
        out = self.send([lead], mail_type=mail_type)
        self.assertEqual((out["sent"], FakeProvider.seen), (0, []))
        return list(out["refused"])

    def test_lead_needs_a_source_and_a_basis(self):
        self.assertEqual(self.refused({"email": "a@x.example", "basis": "opt_in"}), ["missing_source"])
        self.assertEqual(self.refused({"email": "a@x.example", "source": "own_signup"}), ["missing_basis"])

    def test_default_sources_are_own_signup_only(self):
        for bought in ("apollo", "rocketreach", "clearbit", "scraped"):
            self.assertEqual(self.refused({**LEAD, "source": bought}), ["source_not_in_policy"])

    def test_founder_can_add_a_source_by_name(self):
        self.env(FLEET_OUTREACH_SOURCES="own_signup, partner_list")
        self.assertEqual(self.send([{**LEAD, "source": "Partner_List"}])["sent"], 1)

    def test_lifecycle_needs_opt_in_or_customer(self):
        self.assertEqual(self.refused({**LEAD, "basis": "legitimate_interest"}),
                         ["basis_not_allowed_for_lifecycle"])

    def test_bad_address_and_bad_line_are_refused_not_dropped(self):
        self.assertEqual(self.refused({**LEAD, "email": "not-an-address"}), ["bad_email"])
        self.assertEqual(self.refused("line 3"), ["bad_lead"])

    def test_refusals_land_in_the_ledger(self):
        self.refused({**LEAD, "source": "apollo"})
        rows = o.connect().execute("SELECT email, status, reason FROM ledger").fetchall()
        self.assertEqual(rows, [("ann@example.org", "refused", "source_not_in_policy")])


class ColdRefusals(Base):
    def test_cold_is_off_by_default(self):
        self.assertRefused("cold outreach is off", leads=[dict(COLD)], mail_type="cold")

    def test_no_regions_listed_means_no_cold_mail(self):
        self.env(drop={"FLEET_OUTREACH_COLD_REGIONS"}, **self.cold)
        self.assertRefused("no regions allowed", leads=[dict(COLD)], mail_type="cold")

    def test_region_must_be_listed_and_present(self):
        self.env(**self.cold)
        out = self.send([{**COLD, "region": "DE"}, {k: v for k, v in COLD.items() if k != "region"}],
                        mail_type="cold")
        self.assertEqual(out["refused"], {"region_not_in_policy": 1, "missing_region": 1})
        self.assertEqual(FakeProvider.seen, [])

    def test_cold_needs_legitimate_interest_basis_and_a_listed_source(self):
        self.env(**self.cold)
        out = self.send([{**COLD, "basis": "opt_in"}, {**COLD, "source": "apollo"}], mail_type="cold")
        self.assertEqual(out["refused"], {"basis_not_allowed_for_cold": 1, "source_not_in_policy": 1})

    def test_cold_must_not_leave_from_the_product_domain(self):
        for frm in ("Acme <hi@acme.example>", "hi@mail.acme.example", "Acme <hi@ACME.example>"):
            self.env(**{**self.cold, "FLEET_OUTREACH_FROM": frm})
            self.assertRefused("must not leave from the product's domain", leads=[dict(COLD)], mail_type="cold")

    def test_cold_refuses_when_the_product_domain_is_unknown(self):
        self.env(drop={"FLEET_OUTREACH_PRODUCT_DOMAIN"}, **self.cold)
        self.assertRefused("product domain not set", leads=[dict(COLD)], mail_type="cold")

    def test_cold_sends_when_the_founder_allowed_all_of_it(self):
        self.env(**self.cold)
        self.assertEqual(self.send([dict(COLD)], mail_type="cold")["sent"], 1)

    def test_cold_per_person_cap_is_forever_not_weekly(self):
        self.env(**self.cold)
        self.send([dict(COLD)], mail_type="cold")
        conn = o.connect()
        conn.execute("UPDATE ledger SET ts = ts - ?", (30 * o.DAY_S,))
        conn.close()
        self.assertEqual(self.send([dict(COLD)], mail_type="cold")["refused"], {"per_recipient_cap": 1})
        self.env()   # the same age of row no longer blocks lifecycle mail
        conn = o.connect()
        conn.execute("DELETE FROM ledger")
        conn.close()
        self.send()
        conn = o.connect()
        conn.execute("UPDATE ledger SET ts = ts - ?", (8 * o.DAY_S,))
        conn.close()
        self.assertEqual(self.send()["sent"], 1)


class CapsAndSuppression(Base):
    def test_daily_cap_stops_the_run_and_the_next_process(self):
        self.env(FLEET_OUTREACH_DAILY_CAP="2")
        leads = [{**LEAD, "email": f"p{i}@example.org"} for i in range(4)]
        out = self.send(leads)
        self.assertEqual((out["sent"], out["refused"]), (2, {"daily_cap": 2}))
        # a fresh connection (a restart) still sees the cap as used
        self.assertEqual(self.send([{**LEAD, "email": "late@example.org"}])["refused"], {"daily_cap": 1})
        self.assertEqual(len(FakeProvider.seen), 2)

    def test_per_recipient_cap_any_letter_case(self):
        out = self.send([dict(LEAD), {**LEAD, "email": "ANN@Example.ORG"}])
        self.assertEqual((out["sent"], out["refused"]), (1, {"per_recipient_cap": 1}))

    def test_suppressed_address_is_refused_any_letter_case(self):
        conn = o.connect()
        o.suppress(conn, "Ann@Example.org")
        conn.close()
        out = self.send([{**LEAD, "email": "aNN@example.ORG"}])
        self.assertEqual((out["sent"], out["refused"], FakeProvider.seen), (0, {"suppressed": 1}, []))

    def test_suppression_is_checked_at_send_time_not_when_the_run_starts(self):
        def unsubscribe_the_second(_body):
            FakeProvider.on_request = None
            conn = o.connect()
            o.suppress(conn, "second@example.org")
            conn.close()
        FakeProvider.on_request = unsubscribe_the_second
        out = self.send([{**LEAD, "email": "first@example.org"}, {**LEAD, "email": "second@example.org"}])
        self.assertEqual((out["sent"], out["refused"]), (1, {"suppressed": 1}))

    def test_two_sends_racing_cannot_both_pass_a_cap_of_one(self):
        body, env = Path(self.tmp.name) / "body.txt", dict(os.environ, FLEET_OUTREACH_DAILY_CAP="1")
        body.write_text("Hello there")
        procs = []
        for i in range(8):
            leads = Path(self.tmp.name) / f"leads{i}.jsonl"
            leads.write_text(json.dumps({**LEAD, "email": f"racer{i}@example.org"}) + "\n")
            procs.append(subprocess.Popen(
                [sys.executable, str(HERE / "outreach_send.py"), "send", "--type", "lifecycle",
                 "--subject", "Hi", "--body-file", str(body), "--recipients", str(leads), "--send"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
        self.assertEqual(sum(x["sent"] for x in outs), 1, outs)
        self.assertEqual(len(FakeProvider.seen), 1, "the provider must be called exactly once")
        self.assertEqual(sum(x["refused"].get("daily_cap", 0) for x in outs), 7)

    def test_a_crash_mid_send_keeps_its_slot(self):
        self.env(FLEET_OUTREACH_DAILY_CAP="1")
        with mock.patch.object(inbox, "resend_post", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.send()
        self.assertEqual(self.send([{**LEAD, "email": "next@example.org"}])["refused"], {"daily_cap": 1})

    def test_provider_no_gives_the_slot_back_and_stops(self):
        self.env(FLEET_OUTREACH_DAILY_CAP="1")
        FakeProvider.status = 422
        out = self.send([dict(LEAD), {**LEAD, "email": "two@example.org"}])
        self.assertEqual((out["sent"], len(FakeProvider.seen)), (0, 1))
        self.assertIn("422", out["stopped"])
        FakeProvider.status = 200
        self.assertEqual(self.send([{**LEAD, "email": "two@example.org"}])["sent"], 1)

    def test_provider_error_keeps_the_slot_because_it_may_have_gone_out(self):
        self.env(FLEET_OUTREACH_DAILY_CAP="1")
        FakeProvider.status = 500
        self.assertEqual(self.send()["sent"], 0)
        FakeProvider.status = 200
        self.assertEqual(self.send([{**LEAD, "email": "two@example.org"}])["refused"], {"daily_cap": 1})


class RealSend(Base):
    def test_dry_run_is_the_default_and_touches_nothing(self):
        out = self.send(really=False)
        self.assertEqual((out["would_send"], out["sent"], FakeProvider.seen), (1, 0, []))
        self.assertEqual(o.connect().execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 0)

    def test_dry_run_counts_the_daily_cap(self):
        self.env(FLEET_OUTREACH_DAILY_CAP="1")
        out = self.send([dict(LEAD), {**LEAD, "email": "two@example.org"}], really=False)
        self.assertEqual((out["would_send"], out["refused"]), (1, {"daily_cap": 1}))

    def test_send_goes_through_the_kits_one_resend_client_with_the_footer_and_headers(self):
        self.env(FLEET_REPLY_TO="fleet@reply.example")
        self.assertEqual(self.send([{**LEAD, "email": "Ann@Example.org"}])["sent"], 1)
        (req,) = FakeProvider.seen
        self.assertEqual((req["path"], req["auth"], req["ua"]),
                         ("/emails", "Bearer re_test_key", "fleet-kit-inbox/1.0"))
        b = req["body"]
        link = o.unsub_link("ann@example.org", o.policy())
        self.assertEqual((b["from"], b["to"], b["subject"], b["reply_to"]),
                         ("Acme <hi@mail.acme-mail.example>", ["ann@example.org"], "Hello", ["fleet@reply.example"]))
        self.assertTrue(b["text"].startswith("Body text\n\n--\n1 Main St, Springfield\nUnsubscribe: https://"))
        self.assertIn(link, b["text"])
        self.assertEqual(b["headers"], {"List-Unsubscribe": f"<{link}>",
                                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        self.assertEqual(o.connect().execute("SELECT status FROM ledger").fetchall(), [("sent",)])

    def test_cli_is_a_dry_run_without_the_send_flag(self):
        body, leads = Path(self.tmp.name) / "b.txt", Path(self.tmp.name) / "l.jsonl"
        body.write_text("Hello")
        leads.write_text(json.dumps(LEAD) + "\nnot json\n")
        args = ["send", "--type", "lifecycle", "--subject", "Hi", "--body-file", str(body),
                "--recipients", str(leads)]
        with mock.patch("builtins.print") as pr:
            self.assertEqual(o.main(args), 0)
        out = json.loads(pr.call_args.args[0])
        self.assertEqual((out["mode"], out["would_send"], out["refused"]), ("dry-run", 1, {"bad_lead": 1}))
        self.assertEqual(FakeProvider.seen, [])
        self.env(drop={"FLEET_OUTREACH_ENABLED"})
        with mock.patch("builtins.print"):
            self.assertEqual(o.main(args + ["--send"]), 2)

    def test_the_kits_own_reply_mail_is_unchanged_by_the_shared_client(self):
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_kit", "MAIL_FROM": "", "FLEET_REPLY_TO": ""}):
            self.assertTrue(inbox.send_reply("r@example.org", "Re: x", "ok", in_reply_to="<id1>"))
        (req,) = FakeProvider.seen
        self.assertEqual(req["auth"], "Bearer re_kit")
        self.assertEqual(req["body"], {"from": "990 Scout <hello@philanthropy.org>", "to": ["r@example.org"],
                                       "subject": "Re: x", "text": "ok\n\n-- dino fleet",
                                       "headers": {"In-Reply-To": "<id1>", "References": "<id1>"}})
        FakeProvider.status = 500
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_kit"}):
            self.assertFalse(inbox.send_reply("r@example.org", "Re: x", "ok"))


class UnsubscribeRoute(Base):
    def setUp(self):
        super().setUp()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wr.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"
        p = mock.patch.object(wr, "LOG_DIR", Path(self.tmp.name))
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(wr, "LOG_FILE", Path(self.tmp.name) / "wr.log")
        p.start(); self.addCleanup(p.stop)

    def call(self, url, post=False):
        req = urllib.request.Request(url, data=b"List-Unsubscribe=One-Click" if post else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def link(self, email="Ann@Example.org"):
        return o.unsub_link(email, o.policy()).replace("https://fleet.example", self.base)

    def suppressed(self):
        return [r[0] for r in o.connect().execute("SELECT email FROM suppression")]

    def test_the_link_in_the_mail_needs_no_login_and_one_click_stops_mail(self):
        code, page = self.call(self.link())
        self.assertEqual(code, 200)
        self.assertIn("<form method=\"post\">", page)
        self.assertEqual(self.suppressed(), [], "opening the page (or a link scanner) changes nothing")
        code, page = self.call(self.link(), post=True)
        self.assertEqual((code, self.suppressed()), (200, ["ann@example.org"]))
        self.assertEqual(self.send()["refused"], {"suppressed": 1})
        self.assertEqual(FakeProvider.seen, [])

    def test_a_guessed_or_edited_link_does_nothing(self):
        good = self.link()
        for bad in (good[:-1] + ("0" if good[-1] != "0" else "1"),
                    good.replace("ann%40example.org", "bob%40example.org"),
                    f"{self.base}/webhook/unsubscribe", f"{self.base}/webhook/unsubscribe?e=ann@example.org&t="):
            self.assertEqual(self.call(bad, post=True)[0], 400, bad)
            self.assertEqual(self.call(bad)[0], 400, bad)
        self.assertEqual(self.suppressed(), [])

    def test_no_secret_configured_means_no_link_works(self):
        link = self.link()
        self.env(drop={"FLEET_OUTREACH_UNSUB_SECRET"}, FLEET_ENV_FILE="/nonexistent")
        self.assertEqual(self.call(link, post=True)[0], 400)
        self.assertEqual(self.suppressed(), [])

    def test_the_page_does_not_echo_markup(self):
        email = "<script>@x.example"
        url = f"{self.base}/webhook/unsubscribe?" + urllib.parse.urlencode(
            {"e": email, "t": o.unsub_token(email, SECRET)})
        self.assertNotIn("<script>", self.call(url)[1])

    def test_every_other_get_is_still_refused_as_before(self):
        for path in ("/", "/webhook", "/webhook/inbox", "/webhook/run"):
            self.assertEqual(self.call(self.base + path)[0], 501, path)


class MemberIsOffAndFenced(unittest.TestCase):
    def test_growth_ships_disabled_with_no_cron_line(self):
        import member_spec
        spec = member_spec.by_name("growth")
        self.assertIs(spec["enabled"], False)
        entry = (ROOT / "entrypoint.sh").read_text()
        self.assertNotRegex(entry, r"run_member\.sh growth\b|cron_member_enabled growth\b")

    def test_every_member_spec_still_loads(self):
        import member_spec
        self.assertIn("growth", [s["name"] for s in member_spec.load_all()])

    def test_growth_cannot_push_open_prs_or_call_curl(self):
        import member_spec
        deny = member_spec.by_name("growth")["llm"]["tools"]["deny"]
        for rule in ("Bash(git push:*)", "Bash(gh pr create:*)", "Bash(gh pr merge:*)",
                     "Bash(curl:*)", "Bash(wget:*)"):
            self.assertIn(rule, deny)

    def test_runner_drops_the_kits_mail_key_before_growths_model_runs(self):
        text = (HERE / "run_member.sh").read_text()
        line = '[ "$MEMBER" = "growth" ] && unset RESEND_API_KEY'
        self.assertIn(line, text)
        self.assertLess(text.index(line), text.index('log "pass start (kind=llm'))
        r = subprocess.run(["bash", "-c", f'MEMBER="$1"; {line}; echo "${{RESEND_API_KEY:-gone}}"', "x", "growth"],
                           env={**os.environ, "RESEND_API_KEY": "re_live"}, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "gone")
        r = subprocess.run(["bash", "-c", f'MEMBER="$1"; {line}; echo "${{RESEND_API_KEY:-gone}}"', "x", "gru"],
                           env={**os.environ, "RESEND_API_KEY": "re_live"}, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "re_live", "no other member's environment changes")

    def test_charter_is_short_names_no_product_and_reads_the_goal_live(self):
        text = (ROOT / "members" / "growth" / "growth.md").read_text()
        self.assertLess(len(text.splitlines()), 90)
        self.assertIn("docs/VISION.md", text)
        self.assertNotRegex(text.lower(), r"philanthropy|990|dino|nonprofit")
        for never in ("bought", "scraped", "fake", "promotional", "ban it", "ask.py file"):
            self.assertIn(never, text)

    def test_example_settings_leave_everything_off(self):
        live = [ln for ln in (ROOT / "fleet.env.example").read_text().splitlines()
                if ln.startswith("FLEET_OUTREACH_")]
        self.assertEqual(live, [], "every outreach setting ships commented out")


if __name__ == "__main__":
    unittest.main()
