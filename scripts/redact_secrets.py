#!/usr/bin/env python3
"""redact_secrets.py -- shared secret redaction patterns for runs.jsonl and prompts.

Reuses librarian.py's PATTERNS for consistency: one pattern list, not two.
Also adds additional patterns for OpenAI, Stripe, Resend, AWS, Slack, and env vars.

This module is called by:
  - run_report.py: scrub secrets from outcome/evidence/report/reason before writing runs.jsonl
  - north.py / handoff.py: scrub secrets from runs.jsonl when reading back
  - Bash hook: block dangerous credential-exposure commands
"""
from __future__ import annotations

import re

# Same boundary logic as librarian.py to handle JSON-escaped newlines
_BOUNDARY_START = r"(?:(?<!\w)|(?<=\\[nrt]))"

# GitHub tokens (reuse librarian's min length logic)
GITHUB_TOKEN_MIN_LEN = 8

def _simple_sub(cls: str):
    return lambda m: f"[REDACTED:{cls}]"

def _github_token_pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"{_BOUNDARY_START}{prefix}_[A-Za-z0-9]{{{GITHUB_TOKEN_MIN_LEN},255}}\b")

# All patterns: (class_name, regex_pattern, substitution_function)
PATTERNS: list[tuple[str, re.Pattern, "callable"]] = []

# GitHub tokens (5 variants)
for _prefix in ("gho", "ghp", "ghs", "ghu", "ghr"):
    PATTERNS.append((_prefix, _github_token_pattern(_prefix), _simple_sub(_prefix)))

# GitHub PAT format
PATTERNS.append((
    "github_pat",
    re.compile(rf"{_BOUNDARY_START}github_pat_[A-Za-z0-9_]{{{GITHUB_TOKEN_MIN_LEN},255}}\b"),
    _simple_sub("github_pat"),
))

# Anthropic API keys
PATTERNS.append((
    "sk-ant",
    re.compile(rf"{_BOUNDARY_START}sk-ant-[A-Za-z0-9\-_]{{20,}}\b"),
    _simple_sub("sk-ant"),
))

# OpenAI keys (sk- and sk-proj- prefixes)
PATTERNS.append((
    "openai_key",
    re.compile(rf"{_BOUNDARY_START}sk-[A-Za-z0-9]{{{GITHUB_TOKEN_MIN_LEN},255}}\b"),
    _simple_sub("openai_key"),
))
PATTERNS.append((
    "openai_proj_key",
    re.compile(rf"{_BOUNDARY_START}sk-proj-[A-Za-z0-9\-_]{{20,}}\b"),
    _simple_sub("openai_proj_key"),
))

# Postgres credentials
PATTERNS.append((
    "pgpassword",
    re.compile(rf"{_BOUNDARY_START}PGPASSWORD=\S+"),
    _simple_sub("pgpassword"),
))
PATTERNS.append((
    "postgres_url",
    re.compile(rf"{_BOUNDARY_START}postgres(?:ql)?://[^:\s]+:[^@\s]+@\S+"),
    _simple_sub("postgres_url"),
))

# Stripe keys (live, test, and any prefix)
PATTERNS.append((
    "stripe_key",
    re.compile(rf"{_BOUNDARY_START}(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{{{GITHUB_TOKEN_MIN_LEN},}}\b"),
    _simple_sub("stripe_key"),
))

# Webhook signatures (Stripe)
PATTERNS.append((
    "stripe_webhook",
    re.compile(rf"{_BOUNDARY_START}whsec_[A-Za-z0-9]{{{GITHUB_TOKEN_MIN_LEN},}}\b"),
    _simple_sub("stripe_webhook"),
))

# Resend keys
PATTERNS.append((
    "resend_key",
    re.compile(rf"{_BOUNDARY_START}re_[A-Za-z0-9]{{{GITHUB_TOKEN_MIN_LEN},}}\b"),
    _simple_sub("resend_key"),
))

# AWS access key ids (AKIA... format)
PATTERNS.append((
    "aws_access_key",
    re.compile(rf"{_BOUNDARY_START}AKIA[0-9A-Z]{{16}}\b"),
    _simple_sub("aws_access_key"),
))

# AWS secret access keys in connection strings
PATTERNS.append((
    "aws_secret",
    re.compile(rf"{_BOUNDARY_START}aws_secret_access_key=\S+"),
    _simple_sub("aws_secret"),
))

# Slack tokens (xoxb, xoxp, xoxo prefixes)
PATTERNS.append((
    "slack_token",
    re.compile(rf"{_BOUNDARY_START}xox[bopa]-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9\-_]{{24,}}"),
    _simple_sub("slack_token"),
))

# Bearer tokens in URLs and headers
PATTERNS.append((
    "bearer_token",
    re.compile(rf"(?i)bearer\s+[a-z0-9._~+/=-]{{20,}}"),
    lambda m: "bearer [REDACTED:bearer_token]",
))

# Private key blocks (PEM format markers)
PATTERNS.append((
    "private_key",
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE|ENCRYPTED)\s+KEY-----.*?-----END\s+(?:RSA\s+)?(?:PRIVATE|ENCRYPTED)\s+KEY-----", re.DOTALL | re.IGNORECASE),
    _simple_sub("private_key"),
))

# Environment variables ending in _TOKEN, _KEY, _SECRET, _PASSWORD
# Matches: VAR_NAME=<value> where <value> is 12+ chars and not already redacted
PATTERNS.append((
    "secret_env",
    re.compile(
        rf"{_BOUNDARY_START}([A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY)[A-Z0-9_]*)="
        r"(?!\[REDACTED:)(\S{12,})"
    ),
    lambda m: f"{m.group(1)}=[REDACTED:secret_env]",
))

def redact_text(text: str) -> str:
    """Apply all redaction patterns to text. Returns the redacted text."""
    if not text:
        return text
    for _cls, pattern, sub_fn in PATTERNS:
        text = pattern.sub(sub_fn, text)
    return text

def redact_dict_fields(record: dict, fields: list[str]) -> dict:
    """Redact secrets from specific fields in a dict. Returns a copy."""
    result = record.copy()
    for field in fields:
        if field in result and result[field]:
            result[field] = redact_text(str(result[field]))
    return result
