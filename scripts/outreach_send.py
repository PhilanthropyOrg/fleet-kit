#!/usr/bin/env python3
"""outreach_send.py -- the growth member's one door for marketing mail.

Mailing people is a one-way door, so the founder sets a policy ONCE in fleet.env
(FLEET_OUTREACH_*) and this tool refuses anything outside it. There is no per-campaign review
and no flag that skips a check. Everything is off until the founder turns it on: an unset
switch is off, an unset cap is 0, an unset list is empty.

Two kinds of mail:
  lifecycle  to people who signed up with us (basis opt_in or customer).
  cold       to strangers. Off unless FLEET_OUTREACH_COLD_ENABLED=1, and then only to regions
             the founder listed, from a domain that is not the product's.

Every message gets the postal address, an unsubscribe link and the one-click unsubscribe
headers added here; the caller cannot leave them out. The link is served by
webhook_receiver.py (/webhook/unsubscribe) and lands in the suppression list this tool checks
at send time.

Caps are counted from a ledger (outreach.db, next to fleet.db). A send takes its slot inside
one write transaction BEFORE the provider is called, so two sends racing cannot both pass a
cap of 1, and a crash leaves the slot taken, never free.

WHAT THIS IS NOT. Members run as the container's one user with Bash. A member that set out to
get round this tool could (call the provider itself, edit the ledger, change its own
environment). This tool stops mistakes and drift, not a member acting in bad faith. The outer
wall is the provider: give outreach its own Resend key, limited to the sending domain, on a
plan whose quota you can live with. See the growth block in fleet.env.example.

Usage:
  outreach_send.py send --type lifecycle --subject "..." --body-file body.txt \
      --recipients leads.jsonl [--campaign name] [--send]      # dry run unless --send
  outreach_send.py suppress someone@example.com [--why "asked by reply"]
  outreach_send.py status

leads.jsonl: one JSON object per line -- {"email": ..., "source": ..., "basis": ...,
"region": ...}. source = where the address came from; basis = why we may mail it; region is
needed for cold mail. A lead missing any of them is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DAY_S = 86400
WEEK_S = 7 * DAY_S
TYPES = ("lifecycle", "cold")
BASIS = {"lifecycle": {"opt_in", "customer"}, "cold": {"legitimate_interest"}}
DEFAULT_SOURCES = "own_signup"          # the product's own opt-in form; anything else is policy
COUNTED = ("reserved", "sent", "unknown")   # ledger states that use up a cap slot
_EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[a-z0-9-]+(\.[a-z0-9-]+)+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  email    TEXT NOT NULL,
  type     TEXT NOT NULL,
  campaign TEXT,
  source   TEXT,
  basis    TEXT,
  region   TEXT,
  status   TEXT NOT NULL,   -- reserved | sent | failed | unknown | refused
  reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts);
CREATE INDEX IF NOT EXISTS idx_ledger_email ON ledger(email);
CREATE TABLE IF NOT EXISTS suppression (
  email TEXT PRIMARY KEY,   -- always stored lowercased
  ts    REAL NOT NULL,
  why   TEXT
);
"""


class Refused(Exception):
    """The policy does not allow this. The message is the reason, in plain words."""


def norm(email: str) -> str:
    return (email or "").strip().lower()


def db_path() -> Path:
    base = os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit")
    return Path(base) / "outreach.db"


_OPEN_TRIES = 100


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: transactions are ours to open (BEGIN IMMEDIATE in _reserve).
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    # Several sends opening a brand-new ledger at once: switching it to WAL and creating the
    # tables can answer "database is locked" straight away, without waiting out `timeout`
    # (seen on Linux CI 2026-10-01: one of 8 racing sends died here before reserving anything).
    # Setting up the file is safe to repeat, so wait and try again rather than crash.
    for attempt in range(_OPEN_TRIES):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            return conn
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _OPEN_TRIES - 1:
                raise
            time.sleep(0.1)
    return conn


# ------------------------------------------------------------------ policy

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _int(name: str) -> int:
    """An unset, empty or unreadable cap is 0 -- which refuses."""
    try:
        return max(0, int(_env(name)))
    except ValueError:
        return 0


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip().lower() for x in (_env(name) or default).replace(",", " ").split() if x.strip()}


def _domain(addr: str) -> str:
    m = re.search(r"@([A-Za-z0-9.-]+)>?\s*$", addr or "")
    return m.group(1).lower().strip(".") if m else ""


def policy() -> dict:
    return {
        "enabled": _env("FLEET_OUTREACH_ENABLED") == "1",
        "daily_cap": _int("FLEET_OUTREACH_DAILY_CAP"),
        "per_recipient_cap": _int("FLEET_OUTREACH_PER_RECIPIENT_CAP"),
        "from": _env("FLEET_OUTREACH_FROM"),
        "postal": _env("FLEET_OUTREACH_POSTAL_ADDRESS"),
        "unsub_url": _env("FLEET_OUTREACH_UNSUB_URL"),
        "unsub_secret": _env("FLEET_OUTREACH_UNSUB_SECRET"),
        "key_file": _env("FLEET_OUTREACH_KEY_FILE"),
        "sources": _set("FLEET_OUTREACH_SOURCES", DEFAULT_SOURCES),
        "cold_enabled": _env("FLEET_OUTREACH_COLD_ENABLED") == "1",
        "cold_regions": _set("FLEET_OUTREACH_COLD_REGIONS"),
        "product_domain": _env("FLEET_OUTREACH_PRODUCT_DOMAIN").lower().strip("."),
        "reply_to": _env("FLEET_REPLY_TO"),
    }


def check_policy(pol: dict, mail_type: str) -> None:
    """Everything that must be true before ANY message of this type goes out."""
    if mail_type not in TYPES:
        raise Refused(f"type must be one of {', '.join(TYPES)}")
    if not pol["enabled"]:
        raise Refused("outreach is off (FLEET_OUTREACH_ENABLED is not 1)")
    if pol["daily_cap"] <= 0:
        raise Refused("no daily cap set (FLEET_OUTREACH_DAILY_CAP) -- unset means 0")
    if pol["per_recipient_cap"] <= 0:
        raise Refused("no per-person cap set (FLEET_OUTREACH_PER_RECIPIENT_CAP) -- unset means 0")
    if not _domain(pol["from"]):
        raise Refused("no sender address set (FLEET_OUTREACH_FROM)")
    if not pol["postal"]:
        raise Refused("no postal address set (FLEET_OUTREACH_POSTAL_ADDRESS)")
    if not pol["unsub_url"].startswith("https://"):
        raise Refused("no https unsubscribe link set (FLEET_OUTREACH_UNSUB_URL)")
    if len(pol["unsub_secret"]) < 16:
        raise Refused("unsubscribe secret missing or under 16 characters (FLEET_OUTREACH_UNSUB_SECRET)")
    if mail_type == "cold":
        if not pol["cold_enabled"]:
            raise Refused("cold outreach is off (FLEET_OUTREACH_COLD_ENABLED is not 1)")
        if not pol["cold_regions"]:
            raise Refused("no regions allowed for cold outreach (FLEET_OUTREACH_COLD_REGIONS)")
        prod, frm = pol["product_domain"], _domain(pol["from"])
        if not prod:
            raise Refused("product domain not set (FLEET_OUTREACH_PRODUCT_DOMAIN) -- cannot "
                          "check that cold mail leaves from a different domain")
        if frm == prod or frm.endswith("." + prod) or prod.endswith("." + frm):
            raise Refused(f"cold mail must not leave from the product's domain ({prod}); "
                          f"FLEET_OUTREACH_FROM is on {frm}")


def check_lead(pol: dict, mail_type: str, lead: dict) -> str | None:
    """Why this one lead is refused, or None. Suppression and caps are checked at send time."""
    email = norm(lead.get("email", ""))
    if not _EMAIL_RE.match(email):
        return "bad_email"
    source = str(lead.get("source") or "").strip().lower()
    basis = str(lead.get("basis") or "").strip().lower()
    if not source:
        return "missing_source"
    if not basis:
        return "missing_basis"
    if source not in pol["sources"]:
        return "source_not_in_policy"
    if basis not in BASIS[mail_type]:
        return "basis_not_allowed_for_" + mail_type
    if mail_type == "cold":
        region = str(lead.get("region") or "").strip().lower()
        if not region:
            return "missing_region"
        if region not in pol["cold_regions"]:
            return "region_not_in_policy"
    return None


# ------------------------------------------------------------------ unsubscribe

def unsub_token(email: str, secret: str) -> str:
    return hmac.new(secret.encode(), norm(email).encode(), hashlib.sha256).hexdigest()[:32]


def unsub_link(email: str, pol: dict) -> str:
    q = urllib.parse.urlencode({"e": norm(email), "t": unsub_token(email, pol["unsub_secret"])})
    return f"{pol['unsub_url']}?{q}"


def suppress(conn: sqlite3.Connection, email: str, why: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO suppression (email, ts, why) VALUES (?, ?, ?)",
                 (norm(email), time.time(), why[:200]))


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    return conn.execute("SELECT 1 FROM suppression WHERE email = ?", (norm(email),)).fetchone() is not None


def unsubscribe(email: str, token: str, secret: str) -> bool:
    """The web route's whole job: a valid signed link adds the address to the suppression list."""
    if len(secret or "") < 16 or not norm(email):
        return False
    if not hmac.compare_digest(unsub_token(email, secret), token or ""):
        return False
    conn = connect()
    try:
        suppress(conn, email, "unsubscribe link")
    finally:
        conn.close()
    return True


# ------------------------------------------------------------------ sending

def _count(conn, where: str, args: tuple) -> int:
    marks = ",".join("?" * len(COUNTED))
    return conn.execute(f"SELECT COUNT(*) FROM ledger WHERE status IN ({marks}) AND {where}",
                        (*COUNTED, *args)).fetchone()[0]


def cap_reason(conn, pol: dict, mail_type: str, email: str, now: float) -> str | None:
    if is_suppressed(conn, email):
        return "suppressed"
    if _count(conn, "ts > ?", (now - DAY_S,)) >= pol["daily_cap"]:
        return "daily_cap"
    # A stranger is counted over all time; someone who signed up, over the last 7 days.
    since = 0.0 if mail_type == "cold" else now - WEEK_S
    if _count(conn, "email = ? AND ts > ?", (email, since)) >= pol["per_recipient_cap"]:
        return "per_recipient_cap"
    return None


def _log(conn, lead: dict, mail_type: str, campaign: str, status: str, reason: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO ledger (ts, email, type, campaign, source, basis, region, status, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), norm(str(lead.get("email", ""))), mail_type, campaign,
         str(lead.get("source") or ""), str(lead.get("basis") or ""), str(lead.get("region") or ""),
         status, reason))
    return cur.lastrowid


def _reserve(conn, pol: dict, mail_type: str, campaign: str, lead: dict) -> tuple[int | None, str]:
    """Take one cap slot, or say why not. One write transaction: the suppression check, both
    cap counts and the insert happen with the write lock held, so nobody else counts in between."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        reason = cap_reason(conn, pol, mail_type, norm(lead["email"]), time.time())
        if reason:
            _log(conn, lead, mail_type, campaign, "refused", reason)
            conn.execute("COMMIT")
            return None, reason
        row = _log(conn, lead, mail_type, campaign, "reserved")
        conn.execute("COMMIT")
        return row, ""
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def build_payload(pol: dict, email: str, subject: str, body: str) -> dict:
    link = unsub_link(email, pol)
    payload = {
        "from": pol["from"], "to": [norm(email)], "subject": subject,
        "text": f"{body.rstrip()}\n\n--\n{pol['postal']}\nUnsubscribe: {link}\n",
        # RFC 8058 one-click: Gmail and Yahoo require the pair on bulk mail.
        "headers": {"List-Unsubscribe": f"<{link}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    }
    if pol["reply_to"]:
        payload["reply_to"] = [pol["reply_to"]]
    return payload


def _provider_key(pol: dict) -> str:
    """Read from a file, never from the environment: fleet.env is exported to every member's
    model, a file named by FLEET_OUTREACH_KEY_FILE is not."""
    try:
        key = Path(pol["key_file"]).read_text().strip() if pol["key_file"] else ""
    except OSError:
        key = ""
    if not key:
        raise Refused("no provider key (FLEET_OUTREACH_KEY_FILE must name a file holding it)")
    return key


def send(mail_type: str, subject: str, body: str, leads: list[dict], campaign: str = "",
         really: bool = False) -> dict:
    """Dry run unless really=True. Returns {"sent": n, "would_send": n, "refused": {reason: n}}.
    Raises Refused when the policy rules the whole run out."""
    pol = policy()
    check_policy(pol, mail_type)
    if not subject.strip() or "\n" in subject or "\r" in subject:
        raise Refused("subject must be one non-empty line")
    if not body.strip():
        raise Refused("body is empty")
    key = _provider_key(pol) if really else ""
    import inbox  # the kit's one Resend client lives there (resend_post)

    out = {"sent": 0, "would_send": 0, "refused": {}, "stopped": ""}
    conn = connect()

    def refuse(lead, reason, log=True):
        out["refused"][reason] = out["refused"].get(reason, 0) + 1
        if log and really:
            _log(conn, lead, mail_type, campaign, "refused", reason)

    try:
        seen = 0   # dry run only: slots this run would itself use
        for lead in leads:
            reason = check_lead(pol, mail_type, lead) if isinstance(lead, dict) else "bad_lead"
            if reason:
                refuse(lead if isinstance(lead, dict) else {}, reason)
                continue
            email = norm(lead["email"])
            if not really:
                reason = cap_reason(conn, pol, mail_type, email, time.time())
                if not reason and _count(conn, "ts > ?", (time.time() - DAY_S,)) + seen >= pol["daily_cap"]:
                    reason = "daily_cap"
                if reason:
                    refuse(lead, reason)
                else:
                    seen += 1
                    out["would_send"] += 1
                continue
            row, reason = _reserve(conn, pol, mail_type, campaign, lead)
            if row is None:
                refuse(lead, reason, log=False)   # _reserve already wrote the ledger row
                continue
            try:
                code = inbox.resend_post(build_payload(pol, email, subject, body), key)
            except urllib.error.HTTPError as exc:
                code = exc.code
            except Exception:  # noqa: BLE001 -- timeout, DNS, reset: it may have gone out
                code = 0
            if 200 <= code < 300:
                conn.execute("UPDATE ledger SET status='sent' WHERE id=?", (row,))
                out["sent"] += 1
                continue
            # A 4xx is the provider saying no: nothing went out, the slot is given back.
            # Anything else may have gone out, so the slot stays taken.
            status = "failed" if 400 <= code < 500 else "unknown"
            conn.execute("UPDATE ledger SET status=?, reason=? WHERE id=?", (status, f"http_{code}", row))
            out["stopped"] = f"provider answered {code or 'nothing'} -- stopped, nothing more sent this run"
            break
    finally:
        conn.close()
    return out


# ------------------------------------------------------------------ CLI

def _read_leads(path: str) -> list:
    leads = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            leads.append(json.loads(line))
        except json.JSONDecodeError:
            leads.append(f"line {n}")   # counted as bad_lead, never silently dropped
    return leads


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send marketing mail inside the founder's policy.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="dry run unless --send")
    s.add_argument("--type", required=True, choices=TYPES)
    s.add_argument("--subject", required=True)
    s.add_argument("--body-file", required=True)
    s.add_argument("--recipients", required=True, help="JSONL: email, source, basis, region")
    s.add_argument("--campaign", default="")
    s.add_argument("--send", action="store_true", help="really send (default is a dry run)")
    u = sub.add_parser("suppress", help="never mail this address again")
    u.add_argument("email")
    u.add_argument("--why", default="manual")
    sub.add_parser("status", help="the policy in force and today's count (no secrets)")
    args = ap.parse_args(argv)

    if args.cmd == "suppress":
        if not _EMAIL_RE.match(norm(args.email)):
            print("REFUSED: not an email address", file=sys.stderr)
            return 2
        conn = connect()
        suppress(conn, args.email, args.why)
        conn.close()
        print(f"suppressed {norm(args.email)}")
        return 0

    if args.cmd == "status":
        pol = policy()
        conn = connect()
        shown = {k: (sorted(v) if isinstance(v, set) else v) for k, v in pol.items()
                 if k not in ("unsub_secret", "key_file")}
        shown["unsub_secret_set"] = len(pol["unsub_secret"]) >= 16
        shown["key_file_set"] = bool(pol["key_file"])
        shown["sent_last_24h"] = _count(conn, "ts > ?", (time.time() - DAY_S,))
        shown["suppressed"] = conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]
        conn.close()
        for t in TYPES:
            try:
                check_policy(pol, t)
                shown[t] = "allowed"
            except Refused as exc:
                shown[t] = f"refused: {exc}"
        print(json.dumps(shown, indent=2))
        return 0

    try:
        out = send(args.type, args.subject, Path(args.body_file).read_text(),
                   _read_leads(args.recipients), args.campaign, really=args.send)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    out["mode"] = "send" if args.send else "dry-run"
    print(json.dumps(out, indent=2))
    return 1 if out["stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
