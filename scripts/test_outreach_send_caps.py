"""Test outreach_send.py policy enforcement (caps, GDPR, list sources, etc).

Tests verify that each cap and refusal reason actually refuses sends before
they reach a mail provider. Every test FAILS without the feature and PASSES with it.
"""
import os
import sys
import tempfile
import unittest
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402
import outreach_send  # noqa: E402
import member_spec  # noqa: E402


class OutreachSendCapsTest(unittest.TestCase):
    """Test policy enforcement in outreach_send.py."""

    def setUp(self):
        """Set up a temp DB for each test."""
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "fleet.db"
        self.env_backup = dict(os.environ)

    def tearDown(self):
        """Restore env and cleanup."""
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def set_env(self, **kwargs):
        """Set environment variables for the test."""
        os.environ.update(kwargs)

    def connect_db(self):
        """Connect to test DB."""
        os.environ["FLEET_LOG_DIR"] = self.tmp.name
        # Pass db_path explicitly to connect() since DB_FILE is set at module import time
        conn = fleet_db.connect(db_path=self.db_path)
        return conn

    def add_campaign(self, conn, name="test-campaign", email_type="outreach", source="apollo", count=10):
        """Add a test campaign."""
        cur = conn.execute(
            "INSERT INTO outreach_campaigns "
            "(campaign_name, subject, body, email_type, source, recipient_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, "Test Subject", "Test Body", email_type, source, count, time.time())
        )
        conn.commit()
        return cur.lastrowid

    def add_recipient(self, conn, campaign_id, email, region="US", consent="explicit_optin", source="apollo", company=None):
        """Add a test recipient to a campaign."""
        conn.execute(
            "INSERT INTO outreach_recipients "
            "(campaign_id, email, region, consent_status, company, source, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, email, region, consent, company, source, time.time())
        )
        conn.commit()

    def test_policy_disabled_refuses_send(self):
        """Without FLEET_OUTREACH_ENABLED=1, send must refuse."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="0",
            FLEET_OUTREACH_DAILY_CAP="100",
        )
        conn = self.connect_db()
        campaign_id = self.add_campaign(conn)
        self.add_recipient(conn, campaign_id, "test@example.com")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertEqual(result.get("error"), "policy_disabled",
                        "Policy-disabled send must refuse with 'policy_disabled' error")

    def test_daily_cap_enforced(self):
        """Daily send cap must refuse when exceeded."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="5",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
            FLEET_OUTREACH_SENDING_DOMAIN="growth.example.com",
        )
        conn = self.connect_db()

        # Insert 6 sent records from today
        now = time.time()
        for i in range(6):
            conn.execute(
                "INSERT INTO outreach_ledger "
                "(campaign_id, recipient_email, decision, reason, decided_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (999, f"old-{i}@example.com", "sent", "", now)
            )
        conn.commit()

        # Try to send a campaign with 3 more recipients
        campaign_id = self.add_campaign(conn, count=3)
        for i in range(3):
            self.add_recipient(conn, campaign_id, f"new-{i}@example.com")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertEqual(result.get("error"), "daily_cap_exceeded",
                        "Exceeding daily cap must refuse with 'daily_cap_exceeded' error")

    def test_spend_cap_zero_refuses_real_send(self):
        """Real send with $0 spend cap must refuse."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="0",
            FLEET_OUTREACH_SENDING_DOMAIN="growth.example.com",
        )
        conn = self.connect_db()
        campaign_id = self.add_campaign(conn)
        self.add_recipient(conn, campaign_id, "test@example.com")

        # Dry-run should work
        result_dry = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertNotIn("error", result_dry, "Dry-run with $0 cap should work")

        # Real send should fail
        result_real = outreach_send.send_campaign(conn, campaign_id, dry_run=False)
        self.assertEqual(result_real.get("error"), "spend_cap_zero",
                        "Real send with $0 cap must refuse with 'spend_cap_zero' error")

    def test_gdpr_consent_required(self):
        """B2C in GDPR region requires explicit opt-in."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
            FLEET_OUTREACH_GDPR_REGIONS="EU,UK",
            FLEET_OUTREACH_ALLOWED_LIST_SOURCES="apollo",
        )
        conn = self.connect_db()
        campaign_id = self.add_campaign(conn)

        # Add recipient in EU without explicit opt-in
        self.add_recipient(conn, campaign_id, "eu-user@example.com", region="EU", consent="business_contact")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertGreater(result.get("refused", 0), 0,
                          "B2C in GDPR region without explicit opt-in must be refused")
        reasons = result.get("reasons", {})
        self.assertTrue(any("gdpr" in reason for reason in reasons.keys()),
                       f"Refusal reason should mention GDPR, got: {reasons}")

    def test_gdpr_b2b_allowed(self):
        """B2B (with company) in GDPR region is allowed."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
            FLEET_OUTREACH_GDPR_REGIONS="EU,UK",
            FLEET_OUTREACH_ALLOWED_LIST_SOURCES="apollo",
        )
        conn = self.connect_db()
        campaign_id = self.add_campaign(conn)

        # Add B2B recipient in EU (has company field) with business_contact consent
        conn.execute(
            "INSERT INTO outreach_recipients "
            "(campaign_id, email, region, consent_status, company, source, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, "contact@company.eu", "EU", "business_contact", "Company Inc", "apollo", time.time())
        )
        conn.commit()

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertGreater(result.get("sent", 0), 0,
                          "B2B with company in GDPR region should be allowed")

    def test_suppression_list_honored(self):
        """Unsubscribed emails must be refused."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
        )
        conn = self.connect_db()

        # Add email to suppression list
        conn.execute(
            "INSERT INTO outreach_suppression (email, unsubscribed_at, source) "
            "VALUES (?, ?, ?)",
            ("unsubscribed@example.com", time.time(), "manual")
        )
        conn.commit()

        campaign_id = self.add_campaign(conn)
        self.add_recipient(conn, campaign_id, "unsubscribed@example.com")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertGreater(result.get("refused", 0), 0,
                          "Unsubscribed email must be refused")
        self.assertIn("unsubscribed", result.get("reasons", {}),
                     "Refusal reason should mention unsubscribed")

    def test_list_source_validation(self):
        """Only allowed list sources should send."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
            FLEET_OUTREACH_ALLOWED_LIST_SOURCES="apollo,rocketreach",
        )
        conn = self.connect_db()

        # Create campaign with disallowed source
        campaign_id = self.add_campaign(conn, source="bought_list")
        self.add_recipient(conn, campaign_id, "user@example.com", source="bought_list")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertGreater(result.get("refused", 0), 0,
                          "Disallowed source must be refused")

    def test_email_type_validation(self):
        """Only allowed email types should send."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
            FLEET_OUTREACH_ALLOWED_EMAIL_TYPES="transactional,lifecycle",
        )
        conn = self.connect_db()

        # Create campaign with disallowed type
        campaign_id = self.add_campaign(conn, email_type="cold_outreach")
        self.add_recipient(conn, campaign_id, "user@example.com")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertEqual(result.get("error"), "type_not_allowed_cold_outreach",
                        "Disallowed email type must be refused")

    def test_missing_source_refused(self):
        """Recipients without a source must be refused."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
        )
        conn = self.connect_db()

        campaign_id = self.add_campaign(conn)
        # Add recipient with no source
        conn.execute(
            "INSERT INTO outreach_recipients "
            "(campaign_id, email, region, consent_status, source, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (campaign_id, "user@example.com", "US", "explicit_optin", None, time.time())
        )
        conn.commit()

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertGreater(result.get("refused", 0), 0,
                          "Recipient without source must be refused")

    def test_dry_run_logs_ledger(self):
        """Dry-run decisions must be logged to ledger."""
        self.set_env(
            FLEET_OUTREACH_ENABLED="1",
            FLEET_OUTREACH_DAILY_CAP="100",
            FLEET_OUTREACH_SPEND_CAP_USD="100",
        )
        conn = self.connect_db()

        campaign_id = self.add_campaign(conn)
        self.add_recipient(conn, campaign_id, "test@example.com")

        result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)
        self.assertEqual(result.get("sent"), 1, "Dry-run should log one send")

        # Check ledger
        rows = conn.execute(
            "SELECT decision, reason FROM outreach_ledger WHERE campaign_id = ?",
            (campaign_id,)
        ).fetchall()
        self.assertEqual(len(rows), 1, "Dry-run decision should be logged")
        self.assertIn(rows[0][0], ("sent_dryrun", "sent"), "Ledger should show sent or sent_dryrun")

    def test_growth_member_spec_valid(self):
        """Growth member spec must validate."""
        spec_path = Path(HERE).parent / "members" / "growth" / "growth.fleet.json"
        self.assertTrue(spec_path.exists(), f"Growth member spec not found: {spec_path}")

        spec = json.loads(spec_path.read_text())
        self.assertEqual(spec.get("name"), "growth", "Member name must be 'growth'")
        self.assertFalse(spec.get("enabled"), "Growth member must be disabled by default")
        self.assertEqual(spec.get("lane"), "growth", "Member lane must be 'growth'")

        # Validate tool restrictions
        deny_rules = spec.get("llm", {}).get("tools", {}).get("deny", [])
        self.assertTrue(
            any("resend" in r or "smartlead" in r or "postmark" in r for r in deny_rules),
            "Member must deny raw mail API access"
        )


if __name__ == "__main__":
    unittest.main()
