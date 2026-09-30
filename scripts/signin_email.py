#!/usr/bin/env python3
"""signin_email.py -- sign in to the dashboard with an emailed one-time link, no key handed over.

A person on FLEET_OPERATOR_EMAILS types their email on the sign-in box; this mails them a link
to FLEET_VIEW_PUBLIC_URL + "/chat?signin=<token>". The page POSTs the token back
(/api/login_email_verify) and gets a session cookie. So a new operator lets themselves in, and
removing their email from the list cuts them off (the cookie is re-checked against the list on
every write).

Safety, stated so nobody loosens it later:
  - The link's base is ONLY FLEET_VIEW_PUBLIC_URL, never a URL from the request: otherwise
    anyone could get a real token mailed to an operator inside a link to their own site.
  - The link is a page URL; the token is spent by a POST from that page, so a mail scanner
    that GETs every link cannot use it up.
  - Tokens are single-use, expire after TOKEN_TTL_S, and only their hash is kept.
  - The request answers the same whether or not the email is on the list.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path

TOKEN_TTL_S = 15 * 60
RESEND_GAP_S = 60
ALERT_ENV = Path(os.environ.get("FLEET_ALERT_ENV") or "/home/ubuntu/.config/maxx/alert.env")
RESEND_API = os.environ.get("RESEND_API_URL_BASE", "https://api.resend.com")

_TOKENS: dict[str, tuple[str, float]] = {}   # sha256(token) -> (email, expires_at)
_LAST_SENT: dict[str, float] = {}
_LOCK = threading.Lock()


def allowed(values: dict) -> dict[str, str]:
    """email -> name (the part before @) for everyone on FLEET_OPERATOR_EMAILS."""
    raw = values.get("FLEET_OPERATOR_EMAILS") or os.environ.get("FLEET_OPERATOR_EMAILS") or ""
    out = {}
    for e in raw.split(","):
        e = e.strip().lower()
        if re.fullmatch(r"[^@\s,:]+@[^@\s,:]+\.[^@\s,:]+", e):
            out[e] = e.split("@", 1)[0]
    return out


def _alert_env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    if ALERT_ENV.exists():
        for line in ALERT_ENV.read_text(errors="ignore").splitlines():
            m = re.match(rf"^\s*(?:export\s+)?{key}\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip("\"'")
    return ""


def _secret(log_dir: Path) -> bytes:
    """Signing secret for email sessions, made once and kept in the log dir (survives deploys)."""
    path = log_dir / ".signin_email_secret"
    if not path.exists():
        log_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return path.read_text().strip().encode()


def session_cookie(email: str, log_dir: Path) -> str:
    sig = hmac.new(_secret(log_dir), email.encode(), "sha256").hexdigest()
    return f"e:{email}:{sig}"


def who_from_cookie(cookie: str, values: dict, log_dir: Path) -> str:
    """The person's name if this is a valid email session for someone still on the list."""
    if not cookie.startswith("e:"):
        return ""
    email, _, sig = cookie[2:].rpartition(":")
    people = allowed(values)
    if email not in people:
        return ""
    want = hmac.new(_secret(log_dir), email.encode(), "sha256").hexdigest()
    return people[email] if hmac.compare_digest(sig, want) else ""


def send_link(email: str, values: dict, now: float | None = None) -> str:
    """Mail a sign-in link if `email` is allowed. Returns "sent", "skipped" (not on the list,
    or asked again within RESEND_GAP_S) or raises RuntimeError for a config/mail failure."""
    now = time.time() if now is None else now
    email = email.strip().lower()
    base = (values.get("FLEET_VIEW_PUBLIC_URL") or os.environ.get("FLEET_VIEW_PUBLIC_URL") or "").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("email sign-in is off: set FLEET_VIEW_PUBLIC_URL (https://...) in fleet.env")
    if email not in allowed(values):
        return "skipped"
    with _LOCK:
        if now - _LAST_SENT.get(email, 0) < RESEND_GAP_S:
            return "skipped"
        _LAST_SENT[email] = now
        for h in [h for h, (_, exp) in _TOKENS.items() if exp < now]:
            del _TOKENS[h]
        token = secrets.token_urlsafe(32)
        _TOKENS[hashlib.sha256(token.encode()).hexdigest()] = (email, now + TOKEN_TTL_S)
    link = f"{base}/chat?signin={token}"
    payload = {
        "from": _alert_env("MAIL_FROM") or "Fleet <hello@philanthropy.org>",
        "to": [email],
        "subject": "Your fleet dashboard sign-in link",
        "text": (f"Click to sign in to the fleet dashboard:\n\n{link}\n\n"
                 f"The link works once, for {TOKEN_TTL_S // 60} minutes. "
                 "If you didn't ask for it, ignore this email."),
    }
    req = urllib.request.Request(f"{RESEND_API}/emails", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {_alert_env('RESEND_API_KEY')}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "fleet-kit-signin/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception as exc:  # noqa: BLE001 -- any mail failure is reported, never swallowed
        raise RuntimeError(f"could not send the sign-in email: {exc}") from exc
    return "sent"


def redeem(token: str, values: dict, now: float | None = None) -> str:
    """The email a valid, unspent, unexpired token was sent to, or "". Spends the token."""
    now = time.time() if now is None else now
    with _LOCK:
        email, exp = _TOKENS.pop(hashlib.sha256(token.encode()).hexdigest(), ("", 0))
    return email if email and exp >= now and email in allowed(values) else ""
