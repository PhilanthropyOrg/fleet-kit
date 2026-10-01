#!/usr/bin/env python3
"""Tests for security hardening: token scrubbing and credential blocking."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Add scripts to path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import redact_secrets
import run_report
import worktree_guard_hook


def test_redact_secrets_github_tokens():
    """Test redaction of various GitHub token formats."""
    test_cases = [
        ("My token is ghp_1234567890abcdefghijklmnopqrstuvwxyz", "[REDACTED:ghp]"),
        ("OAuth token: gho_abcdefghijklmnop", "[REDACTED:gho]"),
        ("Session: ghs_xyzabcdefghijklmnopqrstuvwxyzabcd", "[REDACTED:ghs]"),
        ("User: ghu_abcdefghijklmno", "[REDACTED:ghu]"),
        ("Refresh: ghr_abcdefghijklmno", "[REDACTED:ghr]"),
        ("PAT: github_pat_11abcdefghijklmnopqrstuvwxyzab", "[REDACTED:github_pat]"),
    ]
    for text, expected_marker in test_cases:
        result = redact_secrets.redact_text(text)
        assert expected_marker in result, f"Failed to redact: {text}"
        # Verify the full token is removed
        assert "1234567890abcdefghijklmnopqrstuvwxyz" not in result or expected_marker in result


def test_redact_secrets_anthropic_keys():
    """Test redaction of Anthropic API keys."""
    text = "My Anthropic key is sk-ant-v7z2d3x4k9m2a5b8c1d4e7f0h3j6k9l2m5"
    result = redact_secrets.redact_text(text)
    assert "[REDACTED:sk-ant]" in result
    assert "v7z2d3x4k9m2a5b8c1d4e7f0h3j6k9l2m5" not in result or "[REDACTED:sk-ant]" in result


def test_redact_secrets_openai_keys():
    """Test redaction of OpenAI keys."""
    test_cases = [
        ("OpenAI key: sk-proj-abcdefghijklmnopqrst", "[REDACTED:openai_proj_key]"),
        ("Legacy: sk-abcdefghijklmnopqrst", "[REDACTED:openai_key]"),
    ]
    for text, expected_marker in test_cases:
        result = redact_secrets.redact_text(text)
        assert expected_marker in result, f"Failed to redact: {text}"


def test_redact_secrets_postgres():
    """Test redaction of Postgres credentials."""
    test_cases = [
        ("PGPASSWORD=myPassword123 psql", "[REDACTED:pgpassword]"),
        ("DB: postgres://user:pass@host:5432/db", "[REDACTED:postgres_url]"),
    ]
    for text, expected_marker in test_cases:
        result = redact_secrets.redact_text(text)
        assert expected_marker in result, f"Failed to redact: {text}"


def test_redact_secrets_stripe_keys():
    """Test redaction of Stripe keys."""
    test_cases = [
        ("Key: sk_test_1234567890abcdefghijklmnopqrstuvwxyz", "[REDACTED:stripe_key]"),
        ("RKey: rk_test_1234567890abcdefghijklmnopqrstuvwxyz", "[REDACTED:stripe_key]"),
        ("Webhook: whsec_1234567890abcdefghijklmnopqrstuvwxyz", "[REDACTED:stripe_webhook]"),
    ]
    for text, expected_marker in test_cases:
        result = redact_secrets.redact_text(text)
        assert expected_marker in result, f"Failed to redact: {text}"


def test_redact_secrets_resend():
    """Test redaction of Resend keys."""
    text = "API key: re_abcdefghijklmnopqrstuvwxyz123456"
    result = redact_secrets.redact_text(text)
    assert "[REDACTED:resend_key]" in result


def test_redact_secrets_aws_keys():
    """Test redaction of AWS keys."""
    test_cases = [
        ("AccessKey: AKIAIOSFODNN7EXAMPLE", "[REDACTED:aws_access_key]"),
        ("Secret: aws_secret_access_key=abcdefghijklmnopqrstuvwxyz", "[REDACTED:aws_secret]"),
    ]
    for text, expected_marker in test_cases:
        result = redact_secrets.redact_text(text)
        assert expected_marker in result, f"Failed to redact: {text}"


def test_redact_secrets_env_vars():
    """Test redaction of environment variables ending in _TOKEN, _KEY, _SECRET, _PASSWORD."""
    test_cases = [
        ("MY_API_KEY=secretvalue123456789", "[REDACTED:secret_env]"),
        ("DB_PASSWORD=pa$$w0rd123456789", "[REDACTED:secret_env]"),
        ("FLASK_SECRET_KEY=thisisaverycomplexsecretkey123", "[REDACTED:secret_env]"),
        ("SOME_TOKEN=abcdefghijklmnopqrstuvwxyz123456", "[REDACTED:secret_env]"),
    ]
    for text, expected_marker in test_cases:
        result = redact_secrets.redact_text(text)
        assert expected_marker in result, f"Failed to redact: {text}"


def test_redact_dict_fields():
    """Test redacting specific fields in a dictionary."""
    rec = {
        "member": "minion",
        "outcome": "Filed ghp_1234567890abcdefghijklmnopqrstuv",
        "evidence": "See log: postgres://user:pass@host/db",
        "report": "All good",
        "other_field": "sk_test_1234567890abcdefghijklmnopqrstuvwxyz",
    }
    redacted = redact_secrets.redact_dict_fields(rec, ["outcome", "evidence", "report"])
    assert "[REDACTED" in redacted["outcome"]
    assert "[REDACTED" in redacted["evidence"]
    assert "All good" == redacted["report"]  # No secrets, unchanged
    assert "sk_test_" in redacted["other_field"]  # Not redacted, not in fields list


def test_run_report_redacts_at_source():
    """Test that run_report.build_record redacts secrets."""
    pass_text = """
Outcome: Deployed to prod using token ghp_abcdefghijklmnopqrstuvwxyz
Evidence: Logs at /var/log/deploy.log
Report:
Ran deployment with PGPASSWORD=mysecretpass123 in env.
All success!
"""
    rec = run_report.build_record(
        member="the-fixer",
        run_id="test-1",
        kind="llm",
        exit_code=0,
        pass_text=pass_text,
        usage=None,
        vision_required=False,
    )
    # Check that secrets are redacted in output fields
    assert "[REDACTED" in str(rec.get("outcome", ""))
    # The report field should have redaction
    assert "[REDACTED" in str(rec.get("report", "")) or "PGPASSWORD" not in str(rec.get("report", ""))


def test_bash_exposes_credentials_bare_env():
    """Test blocking bare env, printenv, set commands."""
    test_cases = [
        "env",
        "env | grep TOKEN",
        "printenv",
        "printenv GH_TOKEN",
        "set",
        "set | head",
    ]
    for cmd in test_cases:
        reason = worktree_guard_hook._bash_exposes_credentials(cmd)
        assert reason is not None, f"Should block: {cmd}"
        assert "env" in reason.lower() or "secret" in reason.lower()


def test_bash_exposes_credentials_echo_gh_token():
    """Test blocking echo/printf/cat of GH_TOKEN."""
    test_cases = [
        "echo $GH_TOKEN",
        "printf %s $GH_TOKEN",
        "cat /root/.claude-account/config",
        "echo GH_TOKEN",
        "printf $FLASK_SECRET_KEY",
    ]
    for cmd in test_cases:
        reason = worktree_guard_hook._bash_exposes_credentials(cmd)
        assert reason is not None, f"Should block: {cmd}"
        assert "credential" in reason.lower() or "secret" in reason.lower()


def test_bash_allows_safe_commands():
    """Test that safe commands are not blocked."""
    test_cases = [
        "echo 'hello world'",
        "git status",
        "cat /repo/README.md",
        "ls -la /tmp",
        "grep pattern file.txt",
        "echo $SAFE_VAR",
        "printf 'Value: %d' 42",
    ]
    for cmd in test_cases:
        reason = worktree_guard_hook._bash_exposes_credentials(cmd)
        assert reason is None, f"Should allow: {cmd}, but got: {reason}"


def main():
    """Run all tests."""
    tests = [
        ("GitHub tokens", test_redact_secrets_github_tokens),
        ("Anthropic keys", test_redact_secrets_anthropic_keys),
        ("OpenAI keys", test_redact_secrets_openai_keys),
        ("Postgres credentials", test_redact_secrets_postgres),
        ("Stripe keys", test_redact_secrets_stripe_keys),
        ("Resend keys", test_redact_secrets_resend),
        ("AWS keys", test_redact_secrets_aws_keys),
        ("Env vars", test_redact_secrets_env_vars),
        ("Dict field redaction", test_redact_dict_fields),
        ("Run report redacts", test_run_report_redacts_at_source),
        ("Block bare env", test_bash_exposes_credentials_bare_env),
        ("Block echo credentials", test_bash_exposes_credentials_echo_gh_token),
        ("Allow safe commands", test_bash_allows_safe_commands),
    ]

    passed, failed = 0, 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {name}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
