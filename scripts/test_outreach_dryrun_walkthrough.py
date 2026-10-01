"""Dry-run walkthrough of outreach_send.py showing caps, ledger, and compliance.

This test demonstrates the feature as a user would use it:
1. Create a campaign with recipients
2. Dry-run to see what would send (and what's refused)
3. Check the ledger to verify all decisions are logged
4. Show footer with unsubscribe link and postal address
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402
import outreach_send  # noqa: E402


def test_dryrun_walkthrough():
    """Walk through a realistic outreach campaign: compose, dry-run, inspect ledger."""
    print("\n" + "=" * 70)
    print("OUTREACH DRY-RUN WALKTHROUGH")
    print("=" * 70)

    # Setup
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "fleet.db"

    # Set policy
    os.environ.update({
        "FLEET_LOG_DIR": tmp.name,
        "FLEET_OUTREACH_ENABLED": "1",
        "FLEET_OUTREACH_DAILY_CAP": "100",
        "FLEET_OUTREACH_SPEND_CAP_USD": "50",
        "FLEET_OUTREACH_SENDING_DOMAIN": "growth.example.com",
        "FLEET_OUTREACH_PHYSICAL_ADDRESS": "123 Main St, Anytown, CA 12345",
        "FLEET_OUTREACH_GDPR_REGIONS": "EU,UK",
        "FLEET_OUTREACH_ALLOWED_LIST_SOURCES": "apollo,own_form",
        "FLEET_OUTREACH_ALLOWED_EMAIL_TYPES": "lifecycle,announcement,outreach",
    })

    conn = fleet_db.connect(db_path=db_path)

    # Step 1: Create a campaign
    print("\n1. CREATE CAMPAIGN")
    print("-" * 70)
    campaign_name = "Q4-Warmup-1"
    print(f"Campaign: {campaign_name}")
    print(f"Type: lifecycle (to people who signed up)")
    print(f"Recipients: 5 test addresses")

    cur = conn.execute(
        "INSERT INTO outreach_campaigns "
        "(campaign_name, subject, body, email_type, source, recipient_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (campaign_name,
         "Ready to get started? Here's your quick start guide.",
         "Hi {name},\n\nWelcome! We're excited you signed up.\n\n[Quick start link]\n\n"
         "Questions? Reply to this email.\n\n"
         "--\n"
         "Growth Team\n"
         "growth.example.com\n"
         "123 Main St, Anytown, CA 12345\n"
         "[Unsubscribe: https://growth.example.com/unsubscribe?token={token}]",
         "lifecycle", "own_form", 5, time.time())
    )
    conn.commit()
    campaign_id = cur.lastrowid
    print(f"✓ Campaign {campaign_id} created")

    # Step 2: Add recipients
    print("\n2. ADD RECIPIENTS")
    print("-" * 70)
    recipients = [
        ("alice@startup.com", "US", "explicit_optin", "Startup Inc", "own_form"),
        ("bob@agency.uk", "UK", "explicit_optin", "Agency Ltd", "own_form"),
        ("charlie@free.eu", "EU", "explicit_optin", None, "own_form"),  # B2C in EU - needs consent
        ("diana@company.de", "DE", "business_contact", "Company GmbH", "apollo"),  # B2B - OK
        ("eve@example.com", "US", "explicit_optin", None, "owned_list"),  # Wrong source - will refuse
    ]

    for email, region, consent, company, source in recipients:
        conn.execute(
            "INSERT INTO outreach_recipients "
            "(campaign_id, email, region, consent_status, company, source, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, email, region, consent, company, source, time.time())
        )
        print(f"  + {email:25} ({region:2}) consent={consent:15} source={source}")
    conn.commit()
    print(f"✓ {len(recipients)} recipients added")

    # Add one to suppression list
    print("\n3. ADD TO SUPPRESSION LIST")
    print("-" * 70)
    print("Adding alice@startup.com to unsubscribe list (previous campaign)")
    conn.execute(
        "INSERT INTO outreach_suppression (email, unsubscribed_at, source) "
        "VALUES (?, ?, ?)",
        ("alice@startup.com", time.time(), "manual")
    )
    conn.commit()

    # Step 4: Dry-run
    print("\n4. DRY-RUN (--dry-run is the default)")
    print("-" * 70)
    print(f"Sending campaign {campaign_id} (dry-run mode)...")
    print()

    result = outreach_send.send_campaign(conn, campaign_id, dry_run=True)

    print()
    print(f"Results: {result['sent']} sent, {result['refused']} refused")
    print("\nRefusal reasons:")
    for reason, count in sorted(result.get("reasons", {}).items()):
        print(f"  {reason:30} {count}")

    # Step 5: Check ledger
    print("\n5. INSPECT LEDGER")
    print("-" * 70)
    print("Every send decision is logged for measurement:")
    print()

    rows = conn.execute(
        "SELECT recipient_email, decision, reason FROM outreach_ledger "
        "WHERE campaign_id = ? ORDER BY decided_at",
        (campaign_id,)
    ).fetchall()

    for email, decision, reason in rows:
        status = "✓ SENT (dry)" if decision == "sent_dryrun" else "✗ REFUSED"
        reason_str = f" ({reason})" if reason else ""
        print(f"  {status:20} {email:25}{reason_str}")

    # Step 6: Show expected footer
    print("\n6. COMPLIANCE CHECK")
    print("-" * 70)
    print("Every email must include:")
    print()
    print("  [Unsubscribe Link]")
    print("  https://growth.example.com/unsubscribe?token=<campaign_id>")
    print()
    print("  [Postal Address (CAN-SPAM required)]")
    print("  123 Main St, Anytown, CA 12345")
    print()
    print("  [From Address]")
    print("  Growth Team <noreply@growth.example.com>")

    # Verify expected behavior
    # 5 recipients: alice (unsubscribed), bob (ok), charlie (ok), diana (ok), eve (wrong source)
    assert result["sent"] == 3, f"Expected 3 sent, got {result['sent']}"
    assert result["refused"] == 2, f"Expected 2 refused, got {result['refused']}"
    assert any("owned_list" in reason for reason in result.get("reasons", {}).keys()), \
        f"Expected 'owned_list' refusal, got: {result.get('reasons', {}).keys()}"
    assert "unsubscribed" in result.get("reasons", {}), \
        f"Expected 'unsubscribed' refusal, got: {result.get('reasons', {}).keys()}"

    # Verify ledger
    sent_count = sum(1 for _, d, _ in rows if d == "sent_dryrun")
    assert sent_count == 3, f"Ledger should show 3 sends, got {sent_count}"

    print("\n" + "=" * 70)
    print("✓ DRY-RUN WALKTHROUGH COMPLETE")
    print("=" * 70)
    print("\nTo send for real (after policy and caps are set correctly):")
    print(f"  python3 scripts/outreach_send.py --campaign-id {campaign_id} --send")
    print()

    tmp.cleanup()


if __name__ == "__main__":
    test_dryrun_walkthrough()
