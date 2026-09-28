"""Reif forwards a mail to the fleet and the fleet acts on what was forwarded.

Job (Reif 2026-09-28): "I usually forward something that came from someone else, so make sure
that can work." Live, 13 of 13 forwards between 09-16 and 09-28 reached the board as his
signature alone: strip_quotes() cut at the forwarded `From:` line, so "Fwd: Page width",
"Fwd: Analytics and Console", "Fwd: MINOR edits to 990 home page", ... were filed as empty items
that gru's gate then parked as needs-spec. The fixtures below are those real mails' shapes,
names and addresses replaced.

Also pinned: the Resend fetch retries a transient failure instead of storing metadata only; a
metadata-only mail is never filed body-less and `refetch` finishes it; the product's own
notifications from hello@ (in FLEET_INBOX_FROM on the box) no longer run the messenger.

Run: python3 scripts/test_inbox_forward.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox  # noqa: E402

SIG = """Pat Owner
Co-Founder
The Example Project Springfield, Missouri m: 555-010-0000 e: pat@owner.example

www.owner.example ( http://www.owner.example )
LinkedIn icon ( https://www.linkedin.com/in/pat/ )

Sent via Superhuman ( https://sprh.mn/?vip=pat@owner.example )
"""

# 09-18 "Fwd: Page width": no note at all, the forwarded message is the request.
PAGE_WIDTH = SIG + """
---------- Forwarded message ----------
From: Vic Partner < vic@partner.example >
Date: Friday, September 18, 2026 at 17:34 CDT
Subject: Page width
To: Pat Owner <pat@site.example>

https://app.screencast.com/abc123

*Vic Partner,* *CEO* | Tampa, FL
"""

# 09-28 daily update: a one-line note above the forward.
DAILY = "Real screenshots of updated work please\n\n" + SIG + """
---------- Forwarded message ----------
From: 990 Scout < hello@site.example >
Date: Monday, September 28, 2026 at 08:02 CDT
Subject: September 28th - daily update (automated)
To: <pat@owner.example>

990 Scout · Daily update
What shipped today
· Profiles can now be verified.
"""

APPLE = """Can we do this?

Begin forwarded message:

From: Dana Donor <dana@donor.example>

Subject: Grant page idea

Date: September 25, 2026 at 9:00:00 AM CDT

To: Pat Owner <pat@owner.example>

Could the grant page show deadlines first?
"""

OUTLOOK = """Please handle.

________________________________
From: Sam Board <sam@board.example>
Sent: Thursday, September 24, 2026 10:12 AM
To: Pat Owner <pat@owner.example>
Subject: Wrong EIN on our page

Our page lists the wrong EIN: it should be 12-3456789.
"""

QUOTED = """fyi

> Begin forwarded message:
>
> From: Lee User <lee@user.example>
> Subject: Unsubscribe
> Date: September 26, 2026
>
> Please take me off this list.
"""

SENDER = "Pat Owner <pat@owner.example>"


class R:
    def __init__(self, out=""):
        self.returncode, self.stdout, self.stderr = 0, out, ""


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        inbox.LOG_DIR = t
        inbox.INBOX, inbox.DONE = t / "inbox.jsonl", t / "inbox.done"
        inbox.RESULTS, inbox.THREADS = t / "inbox.results.jsonl", t / "threads.jsonl"
        self.env = mock.patch.dict(os.environ, {
            "FLEET_INBOX_FROM": "pat@owner.example,hello@site.example",
            "FLEET_INTAKE_FROM": "hello@site.example",
            "FLEET_REPO_URL": "https://github.com/o/r.git"})
        self.env.start()
        self.calls, self.replies = [], []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return R("[]")
        return R("https://github.com/o/r/issues/99\n" if cmd[0] == "gh" else "ok\n")

    def reply(self, to, subject, text, in_reply_to=None):
        self.replies.append((to, subject, text))

    def created(self):
        c = [c for c in self.calls if c[:3] == ["gh", "issue", "create"]]
        self.assertEqual(len(c), 1, self.calls)
        return c[0][c[0].index("--title") + 1], c[0][c[0].index("--body") + 1]

    def mail(self, text, subject="Fwd: Page width", **kw):
        return inbox.store({"id": kw.pop("id", "m1"), "from": SENDER, "subject": subject, "text": text,
                            "message_id": "<x>", **kw}, {})


class SplitForward(Env):
    def test_gmail_marker_no_note(self):
        fwd = inbox.forward_of({"full_text": PAGE_WIDTH, "from": SENDER})
        self.assertEqual(fwd["note"], "")
        self.assertIn("vic@partner.example", fwd["from"])
        self.assertEqual(fwd["subject"], "Page width")
        self.assertTrue(fwd["body"].startswith("https://app.screencast.com/abc123"), fwd["body"])

    def test_note_is_kept_signature_dropped(self):
        fwd = inbox.forward_of({"full_text": DAILY, "from": "pat@owner.example"})
        self.assertEqual(fwd["note"], "Real screenshots of updated work please")
        self.assertIn("What shipped today", fwd["body"])

    def test_apple_mail(self):
        fwd = inbox.forward_of({"full_text": APPLE, "from": SENDER})
        self.assertEqual((fwd["note"], fwd["subject"]), ("Can we do this?", "Grant page idea"))
        self.assertEqual(fwd["body"], "Could the grant page show deadlines first?")

    def test_outlook(self):
        fwd = inbox.forward_of({"full_text": OUTLOOK, "from": SENDER})
        self.assertEqual(fwd["note"], "Please handle.")
        self.assertIn("Thursday", fwd["date"])
        self.assertIn("12-3456789", fwd["body"])

    def test_inline_quoted(self):
        fwd = inbox.forward_of({"full_text": QUOTED, "from": SENDER})
        self.assertEqual((fwd["note"], fwd["subject"], fwd["body"]), ("fyi", "Unsubscribe", "Please take me off this list."))

    def test_plain_reply_is_not_a_forward(self):
        self.assertIsNone(inbox.forward_of({"full_text": "yes 12\n\nOn Mon, Fleet wrote:\n> ask", "from": SENDER}))
        self.assertIsNone(inbox.forward_of({"full_text": "From: here on, stop the person page.", "from": SENDER}))


class ApplyForward(Env):
    def test_forward_files_the_forwarded_request(self):
        row = self.mail(PAGE_WIDTH)
        res = inbox.apply(row, run=self.run_, reply=self.reply)
        self.assertTrue(res["done"])
        title, body = self.created()
        self.assertEqual(title, "Page width")
        self.assertIn("https://app.screencast.com/abc123", body)
        self.assertIn("vic@partner.example", body)
        self.assertNotIn("555-010-0000", body, "Reif's signature is not the request")
        self.assertIn("no note of his own", body)
        to, _, text = self.replies[-1]
        self.assertEqual(to, SENDER)
        self.assertIn("issues/99", text)
        self.assertIn("forwarded from Vic Partner", text)

    def test_note_and_forward_both_reach_the_issue_and_the_receipt(self):
        inbox.apply(self.mail(DAILY, subject="Fwd: September 28th - daily update (automated)"), run=self.run_, reply=self.reply)
        _, body = self.created()
        self.assertIn("Reif's note: Real screenshots of updated work please", body)
        self.assertIn("Profiles can now be verified", body)
        self.assertIn("your note: Real screenshots", self.replies[-1][2])

    def test_html_only_forward(self):
        html = PAGE_WIDTH.replace("\n", "<br>")
        row = inbox.store({"id": "h1", "from": SENDER, "subject": "Fwd: Page width", "html": html}, {})
        inbox.apply(row, run=self.run_, reply=self.reply)
        self.assertIn("https://app.screencast.com/abc123", self.created()[1])

    def test_attachments_are_noted(self):
        row = self.mail(PAGE_WIDTH, attachments=[{"filename": "shot.png", "content_type": "image/png"}])
        self.assertEqual(row["attachments"], ["shot.png"])
        inbox.apply(row, run=self.run_, reply=self.reply)
        self.assertIn("shot.png", self.created()[1])

    def test_forward_without_fwd_subject_is_still_filed(self):
        row = self.mail(OUTLOOK, subject="this one")
        self.assertEqual(row["kind"], "forward")
        inbox.apply(row, run=self.run_, reply=self.reply)
        title, body = self.created()
        self.assertEqual(title, "Wrong EIN on our page")
        self.assertIn("12-3456789", body)

    def test_forwarded_author_cannot_answer_an_ask(self):
        text = "---------- Forwarded message ---------\nFrom: Eve <eve@x.example>\nSubject: hi\n\nyes 12\n"
        inbox.apply(self.mail(text, subject="Fwd: hi"), run=self.run_, reply=self.reply)
        self.assertFalse(any(str(c[1]).endswith("ask.py") for c in self.calls if len(c) > 1), self.calls)

    def test_pending_shows_the_messenger_the_forward(self):
        self.mail(PAGE_WIDTH)
        (p,) = inbox.pending()
        self.assertIn("app.screencast.com", p["forward"]["body"])


class Notifications(Env):
    def test_product_copy_is_not_steering(self):
        row = inbox.store({"id": "n1", "from": "990 Scout <hello@site.example>",
                           "subject": "New reply from 1 Scout", "text": "someone wrote"}, {})
        self.assertEqual(row["kind"], "notification")
        res = inbox.apply(row, run=self.run_, reply=self.reply)
        self.assertTrue(res["done"])
        self.assertEqual((self.calls, self.replies), ([], []))

    def test_a_reply_from_hello_still_steers(self):
        row = inbox.store({"id": "n2", "from": "hello@site.example", "subject": "Re: brief", "text": "stop X"}, {})
        self.assertEqual(row["kind"], "steering")


class Fetch(Env):
    def test_transient_failure_is_retried(self):
        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = b'{"id": "e"}'
        seq = [urllib.error.URLError("Connection reset by peer"), urllib.error.URLError("dns"), body]
        slept = []
        with mock.patch.object(inbox.urllib.request, "urlopen", side_effect=seq):
            got = inbox.fetch_received("e", sleep=slept.append)
        self.assertEqual((got, slept), ({"id": "e"}, [1, 3]))

    def test_auth_failure_is_not_retried(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch.object(inbox.urllib.request, "urlopen", side_effect=[err]) as m:
            with self.assertRaises(urllib.error.HTTPError):
                inbox.fetch_received("e", sleep=lambda s: None)
        self.assertEqual(m.call_count, 1)

    def test_metadata_only_waits_then_refetch_files_it(self):
        row = inbox.store({"id": "f1", "from": SENDER, "subject": "Fwd: Page width", "text": "", "fetch_failed": True}, {})
        res = inbox.apply(row, run=self.run_, reply=self.reply)
        self.assertEqual((res["done"], res.get("deferred"), self.calls), (False, True, []))
        self.assertEqual(inbox.pending(), [], "the messenger does not get a body-less mail")
        full = {"id": "f1", "from": SENDER, "subject": "Fwd: Page width", "text": PAGE_WIDTH}
        out = inbox.refetch(fetch=lambda i: full, launch=lambda: None, run=self.run_, reply=self.reply)
        self.assertEqual(out, ["f1: refetched, done"])
        self.assertIn("app.screencast.com", self.created()[1])
        self.assertEqual(inbox.refetch(fetch=lambda i: full, launch=lambda: None), [], "once is enough")


if __name__ == "__main__":
    unittest.main()
