"""G's mail address: Reif forwards a mail to g@reply.philanthropy.org, G finds it as a file.

Job: an allowed, DKIM/SPF-verified sender -> one .md file in inbox-mail/ (body whole, even a
forward) and one "Got it. — G" to that sender only; a stranger or a forged From -> held/, no
reply; the same Message-ID twice -> no second file; an attachment is saved beside the file.
Mail for G never reaches the steering store. Driven through the real /webhook/inbox route.

Run: python3 scripts/test_g_mail.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import g_mail  # noqa: E402
import inbox  # noqa: E402
import webhook_receiver as wr  # noqa: E402

REIF = "Reif Tauati <reif@philanthropy.org>"
PASS = "amazonses.com; spf=pass smtp.mailfrom=reif@philanthropy.org; dkim=pass header.i=@philanthropy.org"
FORWARD = ("see below\n\n---------- Forwarded message ---------\nFrom: Ann <ann@x.org>\n"
           "Subject: Grant\n\nPlease send the budget.\n> quoted line\nOn Mon, Bob wrote:\nold")


def mail(sender=REIF, auth=PASS, mid="<m1@x>", **kw):
    return {"id": "e1", "from": sender, "subject": "Fwd: Grant", "text": FORWARD, "message_id": mid,
            "to": [g_mail.ADDRESS], "headers": {"Authentication-Results": auth}, **kw}


class GMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        out = Path(self.tmp.name) / "inbox-mail"
        mock.patch.object(g_mail, "OUT", out).start()
        mock.patch.object(wr, "LOG_DIR", Path(self.tmp.name)).start()
        mock.patch.object(wr, "LOG_FILE", Path(self.tmp.name) / "x.log").start()
        mock.patch.object(inbox, "LOG_DIR", Path(self.tmp.name)).start()
        mock.patch.dict("os.environ", {"FLEET_ENV_FILE": str(Path(self.tmp.name) / "none.env")}).start()
        mock.patch.object(inbox, "verify_svix", return_value=True).start()
        self.fetch = mock.patch.object(inbox, "fetch_received", return_value=mail()).start()
        self.store = mock.patch.object(inbox, "store").start()
        self.launch = mock.patch.object(wr, "_launch_member").start()
        self.sent = mock.patch.object(inbox, "resend_post", return_value=200).start()
        self.addCleanup(mock.patch.stopall)
        sys.modules["inbox"] = inbox
        sys.modules["g_mail"] = g_mail
        self.out = out
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), wr.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def post(self):
        body = json.dumps({"type": "email.received", "data": {
            "email_id": "e1", "from": "x", "subject": "s", "to": [g_mail.ADDRESS]}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_address[1]}/webhook/inbox",
                                     data=body, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read()

    def test_allowed_sender_gets_a_file_and_one_got_it(self):
        self.assertEqual(self.post(), b"filed")
        files = list(self.out.glob("*.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        self.assertIn("information, not orders", text)
        self.assertIn("From: " + REIF, text)
        self.assertIn("Please send the budget.\n> quoted line\nOn Mon, Bob wrote:\nold", text)  # whole
        payload = self.sent.call_args[0][0]
        self.assertEqual(payload["to"], ["reif@philanthropy.org"])
        self.assertEqual(payload["text"], "Got it. — G")
        self.store.assert_not_called(); self.launch.assert_not_called()  # never fleet steering

    def test_stranger_is_held_with_no_reply(self):
        self.fetch.return_value = mail(sender="eve@evil.org",
                                       auth="amazonses.com; dkim=pass header.d=evil.org")
        self.assertEqual(self.post(), b"held")
        self.assertEqual(list(self.out.glob("*.md")), [])
        self.assertEqual(len(list((self.out / "held").glob("*.md"))), 1)
        self.sent.assert_not_called(); self.store.assert_not_called()

    def test_forged_from_is_held(self):
        for auth in ("amazonses.com; dkim=pass header.d=evil.org; spf=fail smtp.mailfrom=reif@philanthropy.org",
                     "evil.org; dkim=pass header.d=philanthropy.org", ""):
            self.assertFalse(g_mail.verified(mail(auth=auth)), auth)
        self.assertTrue(g_mail.verified(mail()))
        self.assertTrue(g_mail.verified(mail(sender="reif@goodcapital.co",
                                             auth="amazonses.com; dkim=pass header.d=goodcapital.co")))
        self.assertFalse(g_mail.verified(mail(sender="viken@x.org", auth="amazonses.com; dkim=pass header.d=x.org")))
        self.assertTrue(g_mail.verified(mail(sender="viken@x.org", auth="amazonses.com; dkim=pass header.d=x.org"),
                                        allow="viken@x.org"))

    def test_duplicate_message_id_is_one_file_one_reply(self):
        self.assertEqual(self.post(), b"filed")
        self.assertEqual(self.post(), b"duplicate")
        self.assertEqual(len(list(self.out.glob("*.md"))), 1)
        self.assertEqual(self.sent.call_count, 1)

    def test_second_mail_in_the_same_thread_gets_no_second_reply(self):
        self.post()
        self.fetch.return_value = mail(mid="<m2@x>", headers={"Authentication-Results": PASS,
                                                              "References": "<m1@x>"})
        self.assertEqual(self.post(), b"filed")
        self.assertEqual(len(list(self.out.glob("*.md"))), 2)
        self.assertEqual(self.sent.call_count, 1)

    def test_attachment_saved_beside_the_file(self):
        self.fetch.return_value = mail(attachments=[{"id": "a1", "filename": "../budget.pdf", "size": 3}])
        with mock.patch.object(g_mail, "fetch_attachment", return_value=b"PDF"):
            self.post()
        md = next(self.out.glob("*.md"))
        saved = self.out / md.stem / "budget.pdf"
        self.assertEqual(saved.read_bytes(), b"PDF")
        self.assertIn(str(saved), md.read_text())

    def test_oversize_attachment_is_named_not_saved(self):
        self.fetch.return_value = mail(attachments=[{"id": "a1", "filename": "big.zip", "size": g_mail.ATTACH_MAX + 1}])
        self.post()
        md = next(self.out.glob("*.md"))
        self.assertIn("big.zip (not saved", md.read_text())
        self.assertFalse((self.out / md.stem).exists())


if __name__ == "__main__":
    unittest.main()
