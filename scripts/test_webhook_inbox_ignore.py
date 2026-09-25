"""/webhook/inbox ignores mail addressed only to the support-inbox domain.

Job: Resend webhooks are account-wide, so every support email to inbox@in.philanthropy.org
(prod's inbox) also lands on dino's /webhook/inbox. Those must be acked 200 and dropped: not
stored, no messenger launched, not counted against the untrusted budget. hello@ copies are
BCC'd by a Microsoft 365 rule, so To stays hello@philanthropy.org (an allowlisted, TRUSTED
sender) and only Resend's received_for shows in.philanthropy.org: the steering hole. Reif's replies to fleet@reply.philanthropy.org, and
any mail that also names a non-ignored recipient, are handled exactly as before.

Run: python3 scripts/test_webhook_inbox_ignore.py
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
import inbox  # noqa: E402
import webhook_receiver as wr  # noqa: E402


class IgnoredRecipientTests(unittest.TestCase):
    def test_support_inbox_only_is_ignored(self):
        self.assertTrue(inbox.ignored_recipient({"to": ["inbox@in.philanthropy.org"]}))
        self.assertTrue(inbox.ignored_recipient({"to": ["Support <INBOX@In.Philanthropy.org>"]}))
        self.assertTrue(inbox.ignored_recipient({"to": "inbox@in.philanthropy.org"}))  # single string

    def test_fleet_reply_address_is_handled(self):
        self.assertFalse(inbox.ignored_recipient({"to": ["fleet@reply.philanthropy.org"]}))

    def test_mixed_recipients_are_handled(self):
        self.assertFalse(inbox.ignored_recipient(
            {"to": ["inbox@in.philanthropy.org"], "cc": ["fleet@reply.philanthropy.org"]}))
        self.assertFalse(inbox.ignored_recipient(
            {"to": ["inbox@in.philanthropy.org", "Fleet <fleet@reply.philanthropy.org>"]}))
        self.assertFalse(inbox.ignored_recipient(
            {"to": ["inbox@in.philanthropy.org"], "bcc": "fleet@reply.philanthropy.org"}))

    def test_missing_to_is_handled(self):
        self.assertFalse(inbox.ignored_recipient({}))
        self.assertFalse(inbox.ignored_recipient({"to": []}))
        self.assertFalse(inbox.ignored_recipient({"to": None}))

    def test_bcc_copy_ignored_via_received_for(self):
        # the steering-hole case: To is hello@, only the envelope recipient is the support inbox
        data = {"from": "stranger@x.org", "to": ["hello@philanthropy.org"]}
        self.assertFalse(inbox.ignored_recipient(data))  # webhook data alone can't tell
        email = {"to": ["hello@philanthropy.org"], "received_for": ["inbox@in.philanthropy.org"]}
        self.assertTrue(inbox.ignored_recipient(data, None, email))
        self.assertTrue(inbox.ignored_recipient({**data, "received_for": "inbox@in.philanthropy.org"}))

    def test_received_for_fleet_is_handled(self):
        email = {"to": ["hello@philanthropy.org"], "received_for": ["fleet@reply.philanthropy.org"]}
        self.assertFalse(inbox.ignored_recipient({"to": ["hello@philanthropy.org"]}, None, email))
        email = {"received_for": ["inbox@in.philanthropy.org", "fleet@reply.philanthropy.org"]}
        self.assertFalse(inbox.ignored_recipient({}, None, email))

    def test_domain_match_is_exact_and_configurable(self):
        # a subdomain or look-alike of the ignored domain is not the ignored domain
        self.assertFalse(inbox.ignored_recipient({"to": ["x@evil-in.philanthropy.org"]}))
        self.assertFalse(inbox.ignored_recipient({"to": ["x@philanthropy.org"]}))
        self.assertTrue(inbox.ignored_recipient({"to": ["a@b.com", "c@D.org"]}, "b.com, d.org"))
        self.assertFalse(inbox.ignored_recipient({"to": ["inbox@in.philanthropy.org"]}, ""))


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            mock.patch.object(wr, "LOG_DIR", Path(self.tmp.name)),
            mock.patch.object(wr, "LOG_FILE", Path(self.tmp.name) / "x.log"),
            mock.patch.dict("os.environ", {"FLEET_INBOX_WEBHOOK_SECRET": "whsec_dGVzdA==",
                                           "FLEET_ENV_FILE": str(Path(self.tmp.name) / "none.env")}),
            mock.patch.object(inbox, "verify_svix", return_value=True),
        ]
        self.budget = mock.patch.object(inbox, "untrusted_budget_ok", return_value=True).start()
        self.fetch = mock.patch.object(inbox, "fetch_received", return_value={"id": "e1", "text": "hi"}).start()
        self.store = mock.patch.object(inbox, "store", return_value={"id": "e1"}).start()
        self.apply = mock.patch.object(inbox, "apply", return_value={"done": False}).start()
        self.launch = mock.patch.object(wr, "_launch_member").start()
        self.addCleanup(mock.patch.stopall)
        for p in patches:
            p.start()
        sys.modules["inbox"] = inbox  # the handler does a plain `import inbox`
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), wr.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def post(self, data):
        body = json.dumps({"type": "email.received", "data": {"email_id": "e1", "from": "a@x.org",
                                                               "subject": "s", **data}}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.srv.server_address[1]}/webhook/inbox", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def assert_handled(self, data):
        self.assertEqual(self.post(data), (200, b"stored"))
        self.fetch.assert_called_once(); self.store.assert_called_once()
        self.launch.assert_called_once_with("dont-shoot-the-messenger", ["--task", "inbox"])

    def test_support_inbox_mail_is_dropped(self):
        self.assert_ignored({"to": ["inbox@in.philanthropy.org"]})
        self.fetch.assert_not_called()  # decided from the webhook data alone
        self.assertIn("inbox IGNORED: addressed to in.philanthropy.org", wr.LOG_FILE.read_text())

    def assert_ignored(self, data):
        self.assertEqual(self.post(data), (200, b"ignored"))
        self.store.assert_not_called(); self.apply.assert_not_called(); self.launch.assert_not_called()
        self.budget.assert_not_called()  # never counted against the untrusted hourly cap

    def test_bcc_copy_from_trusted_hello_is_dropped(self):
        # steering hole: From/To hello@ (in FLEET_INBOX_FROM), envelope to the support inbox
        self.fetch.return_value = {"id": "e1", "from": "hello@philanthropy.org", "text": "hi",
                                   "to": ["hello@philanthropy.org"],
                                   "received_for": ["inbox@in.philanthropy.org"]}
        with mock.patch.dict("os.environ", {"FLEET_INBOX_FROM": "hello@philanthropy.org"}):
            self.assert_ignored({"from": "hello@philanthropy.org", "to": ["hello@philanthropy.org"]})
        self.fetch.assert_called_once()

    def test_received_for_fleet_is_handled(self):
        self.fetch.return_value = {"id": "e1", "text": "hi", "to": ["hello@philanthropy.org"],
                                   "received_for": ["fleet@reply.philanthropy.org"]}
        self.assert_handled({"to": ["hello@philanthropy.org"]})

    def test_fetch_failure_without_ignored_recipient_is_handled(self):
        self.fetch.side_effect = RuntimeError("resend down")
        self.assert_handled({"to": ["hello@philanthropy.org"]})

    def test_fleet_reply_is_handled(self):
        self.assert_handled({"to": ["fleet@reply.philanthropy.org"]})

    def test_mixed_recipients_are_handled(self):
        self.assert_handled({"to": ["inbox@in.philanthropy.org", "fleet@reply.philanthropy.org"]})

    def test_missing_to_is_handled(self):
        self.assert_handled({})

    def test_env_overrides_ignored_domains(self):
        with mock.patch.dict("os.environ", {"FLEET_INBOX_IGNORE_TO_DOMAINS": "reply.philanthropy.org"}):
            self.assertEqual(self.post({"to": ["fleet@reply.philanthropy.org"]}), (200, b"ignored"))
            self.assert_handled({"to": ["inbox@in.philanthropy.org"]})


if __name__ == "__main__":
    unittest.main()
