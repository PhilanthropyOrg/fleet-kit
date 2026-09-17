#!/usr/bin/env python3
"""webhook_auth.py -- per-caller bearer tokens for the fleet's webhook receiver (fk#1124).

Reif, 2026-09-17: "it makes sense to make any member of a fleet callable via webhook (given
credentials). In this way agents can rapidly fix things." /api/run_now already Popen's
run_member.sh, but it sits behind the dashboard's one shared FLEET_VIEW_KEY -- no caller
identity, no per-caller revocation. This gives every caller (a canary, CI, another box) its
own token, so a run record can say who fired it and one caller's token can be rotated without
touching anyone else's.

FLEET_WEBHOOK_TOKENS="canary:<tok>,ci:<tok>,dino:<tok>" in fleet.env: a comma list of
`name:token` pairs. caller_for() parses it once per call (this fires per HTTP request, not in
a hot loop) and constant-time-compares the presented token against every configured one, so a
timing side channel cannot narrow down a valid token or which caller it belongs to.

Never log a token value -- log the caller name (once matched) or "unknown", never the header.
"""
from __future__ import annotations

import hmac
import os


def _parse_tokens(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, token = pair.partition(":")
        name, token = name.strip(), token.strip()
        if name and token:
            out[name] = token
    return out


def caller_for(authorization_header: str | None, tokens_env: str | None = None) -> str | None:
    """The configured caller name for a bearer token, or None if the header is missing,
    malformed, or matches no configured token. `tokens_env` defaults to FLEET_WEBHOOK_TOKENS
    (read fresh, not cached, so a rotated token in fleet.env takes effect on the next request
    with no restart -- same convention as FLEET_ENABLED elsewhere in this kit).

    Constant-time: every configured token is compared (never short-circuits on the first
    match) so response timing cannot leak which prefix of a guess is correct.
    """
    header = (authorization_header or "").strip()
    if not header.lower().startswith("bearer "):
        return None
    presented = header[len("bearer "):].strip()
    if not presented:
        return None
    tokens = _parse_tokens(tokens_env if tokens_env is not None else os.environ.get("FLEET_WEBHOOK_TOKENS", ""))
    matched = None
    for name, token in tokens.items():
        if hmac.compare_digest(presented, token):
            matched = name
    return matched
