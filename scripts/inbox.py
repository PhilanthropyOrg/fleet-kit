#!/usr/bin/env python3
"""inbox.py -- Reif replies to the brief; the fleet takes it in (fk#669).

Reif, 2026-09-07: "how do I steer the fleet? idea here is that I can just respond to the email
and it will take those updates in."

The path: every brief is sent with Reply-To fleet@reply.philanthropy.org (a Resend receiving
subdomain). Resend posts an `email.received` webhook to /webhook/inbox on the fleet's
webhook receiver; the receiver verifies the Svix signature, checks the sender is one of
FLEET_INBOX_FROM, fetches the full message from Resend, appends it to
$FLEET_LOG_DIR/inbox.jsonl, and kicks `run_member.sh dont-shoot-the-messenger --task inbox`.
That pass reads the pending replies here, answers the asks they name, turns everything else
into a steering issue the fleet acts on, and emails back what it did.

This module is the deterministic half: signature check, sender allowlist, fetch, store, the
"yes 12 / no 12: why / 12: text" parser, and the pending/done ledger. No model here.

  inbox.py pending            -> JSON list of replies not yet processed
  inbox.py done <id>          -> mark one processed
  inbox.py parse <file>       -> JSON: ask answers + free text found in a reply body (for tests)
  inbox.py apply <id>         -> the deterministic part of one reply (see apply())

fk#1056 (Reif, 2026-09-16: "Getting these via email - can I respond to them via email or how
do I resolve them?"). Two things a reply can carry need no model, so the receiver does them
itself, in seconds, before the messenger's pass even starts: an ask answer (`yes 17`,
`no 17: why`, `17: text`) becomes `ask.py answer`, and a mail whose subject starts with
`backlog:` becomes a `fleet:backlog` issue in the product repo. Either way he gets one short
mail back saying what happened. Only free text that is neither goes on to the messenger.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html as html_mod
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
INBOX = LOG_DIR / "inbox.jsonl"
DONE = LOG_DIR / "inbox.done"
THREADS = LOG_DIR / "threads.jsonl"   # one row per filed issue: where to say "resolved" (fk#1106)
# fk#1129 slice 3: one line per completed mail naming what happened to it -- "board item #N
# created", "comment on #N", "answered ask #N", "steering issue #N", "dropped: <why>". Separate
# file from DONE (a bare id-per-line list `pending()` dedupes against) so this addition cannot
# change DONE's format or `pending()`'s `.split()` parsing of it.
RESULTS = LOG_DIR / "inbox.results.jsonl"
ALERT_ENV = pathlib.Path(os.environ.get("FLEET_ALERT_ENV") or "/home/ubuntu/.config/maxx/alert.env")
ALERT_ENV = pathlib.Path(os.environ.get("FLEET_ALERT_ENV") or "/home/ubuntu/.config/maxx/alert.env")
RESEND_API = os.environ.get("RESEND_API_URL_BASE", "https://api.resend.com")
TOLERANCE_S = 300


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "inbox.log", "a") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] {msg}\n")


# ---------------------------------------------------------------- webhook side

def verify_svix(body: bytes, headers: dict, secret: str, now: float | None = None) -> bool:
    """Standard Svix check (Resend's webhooks): HMAC-SHA256 over "<id>.<timestamp>.<body>"
    keyed by the base64 part of the whsec_ secret; the signature header carries one or more
    "v1,<base64>" entries; the timestamp must be within TOLERANCE_S of now."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sig = h.get("svix-id", ""), h.get("svix-timestamp", ""), h.get("svix-signature", "")
    if not (msg_id and ts and sig and secret):
        return False
    try:
        if abs((now if now is not None else time.time()) - int(ts)) > TOLERANCE_S:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, IndexError):
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    for part in sig.split():
        if "," in part and hmac.compare_digest(part.split(",", 1)[1], expected):
            return True
    return False


def allowed_sender(addr: str, allow: str) -> bool:
    """FLEET_INBOX_FROM is a comma list of addresses; match the bare address inside
    'Name <addr>' too, case-insensitively."""
    m = re.search(r"<([^>]+)>", addr or "")
    bare = (m.group(1) if m else (addr or "")).strip().lower()
    return bare in {a.strip().lower() for a in (allow or "").split(",") if a.strip()}


# fk#1129 slice 1: the intake epic ("there should be a central input dump where all the data
# inputs can go via email, webhooks, etc.") starts here -- widen who is ACCEPTED without
# widening who can STEER. FLEET_INBOX_FROM (Reif's own addresses) stays the only allowlist
# that can answer an ask or write free text into steering; FLEET_INTAKE_FROM is everything
# else the fleet already knows how to triage on its own: the product's own notifier, and the
# two machine senders that page/notify by mail today. Bare address, same matching as
# allowed_sender() above.
INTAKE_FROM_DEFAULT = "hello@philanthropy.org,noreply@digitalocean.com,support@digitalocean.com,notifications@github.com"


def bare_address(addr: str) -> str:
    m = re.search(r"<([^>]+)>", addr or "")
    return (m.group(1) if m else (addr or "")).strip().lower()


def intake_allowed(addr: str, allow: str | None = None) -> bool:
    """FLEET_INTAKE_FROM (default INTAKE_FROM_DEFAULT): accepted and stored, but never treated
    as a steering reply -- no ask-answer parsing, no free-text-to-steering path."""
    allow = os.environ.get("FLEET_INTAKE_FROM", INTAKE_FROM_DEFAULT) if allow is None else allow
    return bare_address(addr) in {a.strip().lower() for a in (allow or "").split(",") if a.strip()}


def resend_key() -> str:
    if os.environ.get("RESEND_API_KEY"):
        return os.environ["RESEND_API_KEY"]
    if ALERT_ENV.exists():
        for line in ALERT_ENV.read_text().splitlines():
            m = re.match(r"^\s*(?:export\s+)?RESEND_API_KEY\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip("\"'")
    return ""


def fetch_received(email_id: str) -> dict:
    req = urllib.request.Request(f"{RESEND_API}/emails/receiving/{email_id}",
                                 headers={"Authorization": f"Bearer {resend_key()}",
                                          "User-Agent": "fleet-kit-inbox/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def strip_quotes(text: str) -> str:
    """Drop the quoted brief under the reply: everything from the first quote marker or
    'On ... wrote:' line on. Keeps what Reif typed."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:\s*$", line) or line.strip() in ("-- ", "--"):
            break
        if re.match(r"^\s*(From|Sent|To|Subject):\s", line) and out:
            break
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(h: str) -> str:
    t = re.sub(r"<(br|/p|/div|/tr)[^>]*>", "\n", h or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html_mod.unescape(t)


UNTRUSTED_PER_HOUR = int(os.environ.get("FLEET_INBOX_UNTRUSTED_PER_HOUR") or 20)


def untrusted_budget_ok(now: float | None = None) -> bool:
    """At most UNTRUSTED_PER_HOUR untrusted mails stored per hour; the rest are dropped."""
    now = now or time.time()
    n = 0
    try:
        for line in INBOX.read_text().splitlines()[-2000:]:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("trusted") is False and (r.get("received_at") or 0) > now - 3600:
                n += 1
    except OSError:
        pass
    return n < UNTRUSTED_PER_HOUR


def store(email: dict, event: dict, trusted: bool = True) -> dict:
    text = email.get("text") or html_to_text(email.get("html") or "")
    sender = email.get("from") or event.get("from")
    subject = email.get("subject") or event.get("subject")
    mail = {"from": sender, "subject": subject, "text": strip_quotes(text)}
    row = {
        "id": email.get("id") or event.get("email_id"),
        "received_at": time.time(),
        "trusted": bool(trusted),
        "from": sender,
        "subject": subject,
        "text": mail["text"],
        "full_text": text[:20000],
        "in_reply_to": (email.get("headers") or {}).get("in-reply-to") or (email.get("headers") or {}).get("In-Reply-To"),
        "message_id": email.get("message_id"),
        "kind": classify(mail),
        "source": bare_address(sender),
    }
    check = alert_check(subject)
    if check:
        row["check"] = check
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(INBOX, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    log(f"stored reply {row['id']} from {row['from']} subject={row['subject']!r} chars={len(row['text'])} kind={row['kind']}")
    return row


# ---------------------------------------------------------------- pass side

ANSWER_RE = re.compile(r"^\s*(?:(yes|no|ok|approve[d]?|go|do it)\s+#?(\d+)\s*[:\-–]?\s*(.*)|#?(\d+)\s*[:\-–]\s*(.+))\s*$", re.I)


def parse_reply(text: str) -> dict:
    """Lines like 'yes 12', 'no 12: too expensive', '12: send it Tuesday' answer ask 12.
    Everything else is free text for the messenger to turn into steering."""
    answers, free = [], []
    for line in strip_quotes(text or "").splitlines():
        m = ANSWER_RE.match(line)
        if m and (m.group(2) or m.group(4)):
            if m.group(2):
                word = m.group(1).lower()
                verdict = "no" if word == "no" else "yes"
                answers.append({"ask_id": int(m.group(2)), "answer": (verdict + (": " + m.group(3).strip() if m.group(3).strip() else ""))})
            else:
                answers.append({"ask_id": int(m.group(4)), "answer": m.group(5).strip()})
        elif line.strip():
            free.append(line.rstrip())
    return {"answers": answers, "free_text": "\n".join(free).strip()}


# ---------------------------------------------------------------- apply (fk#1056)

BACKLOG_RE = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)*backlog\s*:\s*(.+?)\s*$", re.I)
# A forward from an allowlisted sender is a backlog item by definition (Reif 2026-09-16: "do we
# have an email yet that I can forward things to that goes to the backlog") -- nobody forwards
# a mail to the fleet to chat. A prod alert from the box's own pager (page.py) is the same
# thing filed by a machine: title keeps the check name, and it lands priority-high on devops.
FORWARD_RE = re.compile(r"^\s*(?:(?:re)\s*:\s*)*(?:fwd?|fw)\s*:\s*(.+?)\s*$", re.I)
PROD_ALERT_RE = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)*990 scout prod alert\s*(\[[^\]]+\]\s*:.+?)\s*$", re.I)


def backlog_title(subject: str) -> str | None:
    """'backlog: fix the claim button' -> 'fix the claim button'; 'Fwd: anything' -> 'anything';
    '990 Scout prod alert [app_error]: 20 timeouts' -> 'prod alert [app_error]: 20 timeouts';
    anything else -> None."""
    for rx, prefix in ((BACKLOG_RE, ""), (PROD_ALERT_RE, "prod alert "), (FORWARD_RE, "")):
        m = rx.match(subject or "")
        if m:
            return prefix + m.group(1)
    return None


def backlog_labels(title: str) -> str:
    return "fleet:backlog,fleet:priority-high,lane:devops" if title.startswith("prod alert [") else "fleet:backlog"


# fk#1129 slice 1: classification is the deterministic front door every mail passes through
# before triage decides anything. One kind, from sender + subject + body alone -- no model,
# so it never blocks the receiver and every kind stays testable with a dict literal.
QUESTION_SUBJECT_RE = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)*(?:new question from|.+ messaged you on 990 scout)\b", re.I)
ALERT_CHECK_RE = re.compile(r"990 scout prod alert\s*\[([^\]]+)\]", re.I)

GITHUB_SENDER_RE = re.compile(r"@(?:notifications\.)?github\.com$", re.I)
DIGITALOCEAN_SENDER_RE = re.compile(r"@(?:noreply\.)?digitalocean\.com$", re.I)


def alert_check(subject: str) -> str | None:
    """'990 Scout prod alert [app_error]: 20 timeouts' -> 'app_error'; else None."""
    m = ALERT_CHECK_RE.search(subject or "")
    return m.group(1).strip() if m else None


def classify(mail: dict) -> str:
    """kind for one mail, from its from/subject/text alone: ask_answer | steering | question |
    alert | forward | github | monitoring | unknown. Reif's own addresses (FLEET_INBOX_FROM)
    can steer; everyone else lands on the fixed machine kinds or unknown -- never steering,
    never an ask answer, however the text is shaped (an untrusted sender writing "yes 12" is
    not Reif answering ask 12)."""
    sender = mail.get("from") or ""
    subject = mail.get("subject") or ""
    bare = bare_address(sender)
    reif = {a.strip().lower() for a in (os.environ.get("FLEET_INBOX_FROM") or "").split(",") if a.strip()}
    if alert_check(subject):
        return "alert"
    if QUESTION_SUBJECT_RE.match(subject):
        return "question"
    if GITHUB_SENDER_RE.search(bare):
        return "github"
    if DIGITALOCEAN_SENDER_RE.search(bare):
        return "monitoring"
    if bare in reif:
        if FORWARD_RE.match(subject):
            return "forward"
        text = mail.get("text") or ""
        return "ask_answer" if parse_reply(text)["answers"] else "steering"
    if intake_allowed(sender):
        return "unknown"
    return "unknown"


def repo_slug() -> str:
    url = (os.environ.get("FLEET_REPO_URL") or "").strip()
    slug = url.rsplit("github.com", 1)[-1].lstrip(":/").removesuffix(".git").strip("/")
    return slug if slug.count("/") == 1 and all(slug.split("/")) else ""


def answer_asks(answers: list[dict], run=None) -> list[str]:
    """Each parsed answer -> `ask.py answer <id> --answer ... --answered-by "reif (email)"`.
    Returns one plain line per ask. An already-answered ask is ask.py's own no-op."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    out = []
    ask_py = pathlib.Path(__file__).resolve().parent / "ask.py"
    for a in answers:
        r = run([sys.executable, str(ask_py), "answer", str(a["ask_id"]),
                 "--answer", a["answer"], "--answered-by", "reif (email)"])
        ok = r.returncode == 0
        out.append(f"ask #{a['ask_id']}: {'answered' if ok else 'NOT answered'} -- {a['answer']}"
                   + ("" if ok else f" ({(r.stderr or r.stdout).strip()[:120]})"))
    return out


def open_issue_titled(slug: str, title: str, run) -> dict | None:
    """The one open board item whose title equals `title` (case-insensitive), or None.

    REST list, never the search flag: the search API is a separate, much smaller rate-limit
    bucket (30/min per token, shared with every other member's searches) and its index lags
    a fresh issue by seconds to minutes. Both failure shapes were live on 2026-09-19: the
    hourly `[app_error]` pager filed a NEW issue every hour for three hours with the previous
    hour's twin still open (#6863/#6866/#6870), and the box copy and the mailed copy of the
    same firing, 4s apart, each filed their own (#6870/#6871). The REST list is read-after-
    write consistent and draws on the 5000/hr core bucket, so the second copy sees the first.
    A failed lookup is LOGGED (it used to fall through to `create` in silence, which is how
    the duplicates hid) and still returns None: filing a twin beats dropping a pager."""
    r = run(["gh", "issue", "list", "--repo", slug, "--state", "open",
             "--json", "number,title,url", "--limit", "300"])
    if r.returncode != 0:
        log(f"dedupe lookup failed (rc={r.returncode}): {(r.stderr or r.stdout or '').strip()[:200]}")
        return None
    try:
        for it in json.loads(r.stdout or "[]"):
            if (it.get("title") or "").strip().lower() == title.strip().lower():
                it.setdefault("url", f"https://github.com/{slug}/issues/{it['number']}")
                return it
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log(f"dedupe lookup unreadable: {exc}")
    return None


def file_backlog(title: str, body: str, sender: str, run=None) -> str:
    """`gh issue create` in the product repo, label fleet:backlog. Returns the issue URL."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    slug = repo_slug()
    if not slug:
        raise RuntimeError("FLEET_REPO_URL does not name a GitHub repo")
    text = (body or "").strip() or title
    # Dedupe by exact open title: a pager that fires hourly must not file hourly. The repeat
    # becomes a comment on the open item (so the count is visible), never a twin.
    it = open_issue_titled(slug, title, run)
    if it:
        run(["gh", "issue", "comment", "--repo", slug, str(it["number"]),
             "--body", f"Fired again by email from {sender}:\n\n{text[:1500]}"])
        return it["url"]
    r = run(["gh", "issue", "create", "--repo", slug, "--label", backlog_labels(title),
             "--title", title, "--body", f"{text}\n\nFiled by email from {sender} (fk#1056)."])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout.strip().splitlines()[-1]


def send_reply(to: str, subject: str, text: str, in_reply_to: str | None = None) -> bool:
    """One short confirmation back to the sender, threaded under his mail."""
    key = resend_key()
    if not (key and to):
        return False
    payload = {"from": os.environ.get("MAIL_FROM") or "990 Scout <hello@philanthropy.org>",
               "to": [to], "subject": subject, "text": text + "\n\n-- dino fleet"}
    if os.environ.get("FLEET_REPLY_TO"):
        payload["reply_to"] = [os.environ["FLEET_REPLY_TO"]]
    if in_reply_to:
        payload["headers"] = {"In-Reply-To": in_reply_to, "References": in_reply_to}
    req = urllib.request.Request(f"{RESEND_API}/emails", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "fleet-kit-inbox/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300
    except Exception as exc:  # noqa: BLE001
        log(f"reply to {to} failed: {exc}")
        return False


def reif_address() -> str:
    """Where a receipt goes when the sender is a machine: FLEET_ALERT_EMAIL (env, then the
    alert env file the brief uses), else the first human in FLEET_INBOX_FROM."""
    v = (os.environ.get("FLEET_ALERT_EMAIL") or "").strip()
    if not v and ALERT_ENV.exists():
        for line in ALERT_ENV.read_text().splitlines():
            if line.startswith("FLEET_ALERT_EMAIL="):
                v = line.split("=", 1)[1].strip().strip('"')
    if not v:
        v = (os.environ.get("FLEET_INBOX_FROM") or "").split(",")[0].strip()
    return v.split(",")[0].strip()


def notify_address(sender: str) -> str:
    """Reif 2026-09-16: "no response back to the thread so that I can't know if there was some
    resolution." A pager mail comes FROM the box (hello@philanthropy.org); replying to it tells
    nobody. The receipt and the resolution go to Reif, threaded under the same message."""
    bare = (re.search(r"<([^>]+)>", sender or "") or [None, sender or ""])[1].strip().lower()
    humans = {a.strip().lower() for a in (os.environ.get("FLEET_INBOX_FROM") or "").split(",") if a.strip()}
    if not humans or bare in humans:
        return sender
    return reif_address() or sender


def record_thread(url: str, row: dict, to: str) -> None:
    m = re.search(r"/issues/(\d+)", url or "")
    if not m:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(THREADS, "a") as fh:
        fh.write(json.dumps({"issue": int(m.group(1)), "url": url, "to": to, "subject": row.get("subject") or "",
                             "message_id": row.get("message_id"), "filed_at": time.time()}) + "\n")


def open_threads() -> list[dict]:
    """Thread rows not yet resolved (a row with `resolved_at` closes the same issue number)."""
    rows, resolved = [], set()
    try:
        for line in THREADS.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("resolved_at"):
                resolved.add(r.get("issue"))
            else:
                rows.append(r)
    except OSError:
        pass
    return [r for r in rows if r.get("issue") not in resolved]


def resolution_text(issue: dict) -> str:
    """What closed it, in the words already on the issue: the closing PR, else the last comment."""
    prs = [p for p in (issue.get("closedByPullRequestsReferences") or []) if p.get("title")]
    if prs:
        p = prs[-1]
        return f"Fixed by PR #{p.get('number')}: {p.get('title')}\n{p.get('url') or ''}".strip()
    comments = issue.get("comments") or []
    if comments:
        return (comments[-1].get("body") or "").strip()[:800]
    reason = issue.get("stateReason") or "closed"
    return f"Closed ({reason.lower().replace('_', ' ')}) with no comment."


def resolve(run=None, reply=None, now: float | None = None) -> list[str]:
    """For every filed thread whose issue has since closed: one 'Resolved' reply on the
    original mail thread, then the thread row is marked. Deterministic, idempotent."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    reply = reply or send_reply
    now = now or time.time()
    slug = repo_slug()
    out = []
    for t in open_threads():
        r = run(["gh", "issue", "view", str(t["issue"]), "--repo", slug, "--json",
                 "state,stateReason,closedAt,title,url,comments,closedByPullRequestsReferences"])
        if r.returncode != 0:
            continue
        try:
            issue = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            continue
        if (issue.get("state") or "").upper() != "CLOSED":
            continue
        how = resolution_text(issue)
        text = f"Resolved: {issue.get('title') or t.get('subject')}\n\n{how}\n\n{issue.get('url') or t.get('url')}"
        sent = reply(t.get("to") or reif_address(), f"Re: {t.get('subject') or 'your report'}", text, t.get("message_id"))
        with open(THREADS, "a") as fh:
            fh.write(json.dumps({**t, "resolved_at": now, "sent": bool(sent)}) + "\n")
        out.append(f"#{t['issue']} resolved -> {t.get('to')}")
        log(f"resolved #{t['issue']}: replied to {t.get('to')} ({'sent' if sent else 'NOT sent'})")
    return out


def alert_title(check: str) -> str:
    return f"prod alert [{check}]"


def file_or_comment_alert(row: dict, run=None) -> tuple[str, bool]:
    """fk#1129 slice 1: one open board item per `check`, ever -- a firing that finds it open
    appends ONE comment with the new firing's first line and stops; only a `check` with no
    open item yet creates one, priority-low on devops (gh#6575: an automated filer must not
    assign the top tier -- that is marie's call, not a pager's). Same search-by-title shape as
    file_backlog() (dedupe by exact open title), split out because an alert is keyed by
    `check`, not by the mail's subject verbatim (two firings of the same check rarely share
    an identical subject line -- differing counts, timestamps).

    Returns (url, created) -- fk#1129 slice 3: `created` lets apply() say "board item #N
    created" the first time and "comment on #N" every firing after, instead of one word for
    both."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    slug = repo_slug()
    if not slug:
        raise RuntimeError("FLEET_REPO_URL does not name a GitHub repo")
    title = alert_title(row["check"])
    first_line = (row.get("text") or row.get("subject") or "").strip().splitlines()[0:1]
    first_line = first_line[0] if first_line else row.get("subject") or ""
    it = open_issue_titled(slug, title, run)
    if it:
        run(["gh", "issue", "comment", "--repo", slug, str(it["number"]),
             "--body", f"Fired again: {first_line}"[:1500]])
        return it["url"], False
    # fk#1158: an alert item was filed with no Vision-link line and no severity label, so
    # vision_link_gate classified it `missing` and dropped it from every claim pack -- filed,
    # never built, then closed by marie as a twin (~$9 a pass). A firing pager IS "an active,
    # ongoing failure" (the label's own description); the box's health check detected it, not
    # a model reading issue text. Filed as `none (maintenance)` + severity-live so it survives
    # the crowding-out drop while it is firing; marie clears the label when it stops.
    # gh#6575: priority-low, not priority-high -- the top tier is marie's to assign after she
    # has scored the item, not a default an unattended pager hands itself. severity-live above
    # is what keeps a genuinely firing alert visible despite the low tier (gh#726's escape
    # hatch keys off severity-live + a linked candidate, not off priority).
    r = run(["gh", "issue", "create", "--repo", slug,
             "--label", "fleet:backlog,lane:devops,fleet:priority-low,fleet:severity-live",
             "--title", title,
             "--body", f"{first_line}\n\nFiled by intake from {row.get('from') or row.get('source')} (fk#1129).\n\n"
                       f"Vision-link: none (maintenance)"])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout.strip().splitlines()[-1], True


def apply(row: dict, run=None, reply=None) -> dict:
    """The no-model half of one stored mail. Returns {"lines": [...], "free_text": str,
    "done": bool}: done means nothing is left for the messenger and the row is marked.

    fk#1129 slice 1: routes by `kind` first, machine kinds regardless of `trusted` --
    otherwise an alert mailed by the box's own pager (never Reif, so always untrusted under
    the old gate) sat pending forever waiting for a human to read it by hand. `question` is
    already surfaced by the product itself, so it's marked done with no action; `github` and
    `monitoring` are kept (done, no action) for a later digest pass (fk#1129 PR 3) rather than
    filed sight unseen. Only `ask_answer`/`steering` ever touch an ask or steering -- gated on
    `trusted` exactly as before, so a stranger can never answer ask #17 by guessing the shape."""
    reply = reply or send_reply
    kind = row.get("kind") or classify(row)
    if kind == "alert" and row.get("check"):
        result = f"dropped: {row['check']} NOT filed"
        try:
            url, created = file_or_comment_alert(row, run=run)
            m = re.search(r"/issues/(\d+)", url or "")
            num = m.group(1) if m else "?"
            result = f"board item #{num} created" if created else f"comment on #{num}"
            log(f"applied {row.get('id')}: alert[{row['check']}] -> {url}")
        except Exception as exc:  # noqa: BLE001
            log(f"applied {row.get('id')}: alert[{row.get('check')}] NOT filed: {exc}")
        if row.get("id"):
            mark_done(row["id"], result=result)
        return {"lines": [], "free_text": "", "done": True}
    if kind in ("question", "github", "monitoring"):
        # Logged only: question is already notified by the product; github/monitoring are
        # kept for the morning-brief digest a later PR reads, not filed as board items here.
        result = "dropped: already handled by the product" if kind == "question" else "dropped: no action needed"
        if row.get("id"):
            mark_done(row["id"], result=result)
        return {"lines": [], "free_text": "", "done": True}
    if row.get("trusted") is False:
        # Not Reif, and not one of the machine kinds above: no ask gets answered and nothing
        # is filed by rule. The messenger reads it (garbage -> `inbox.py done`, real -> it
        # files with the sender named) and no receipt goes back, so a stranger learns nothing
        # about the fleet from mailing it.
        return {"lines": [], "free_text": row.get("text") or "", "done": False}
    parsed = parse_reply(row.get("text") or "")
    lines = answer_asks(parsed["answers"], run=run)
    results = [f"answered ask #{a['ask_id']}" for a in parsed["answers"]]
    free = parsed["free_text"]
    title = backlog_title(row.get("subject") or "")
    to = notify_address(row.get("from") or "")
    if title:
        try:
            url = file_backlog(title, free, row.get("from") or "", run=run)
            lines.append(f"backlog item filed: {url}")
            record_thread(url, row, to)
            m = re.search(r"/issues/(\d+)", url or "")
            num = m.group(1) if m else "?"
            results.append(f"steering issue #{num}" if kind == "forward" else f"board item #{num} created")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"backlog item NOT filed: {exc}")
            results.append(f"dropped: filing failed ({exc})")
        free = ""
    done = not free
    if lines:
        reply(to, f"Re: {row.get('subject') or 'your reply'}",
              "\n".join(lines) + ("" if done else "\n\nThe rest of your note went to the messenger."),
              row.get("message_id"))
        log(f"applied {row.get('id')}: " + "; ".join(lines))
    if done and row.get("id"):
        mark_done(row["id"], result="; ".join(results) if results else "")
    return {"lines": lines, "free_text": free, "done": done}


def pending() -> list[dict]:
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    rows = []
    if INBOX.exists():
        for line in INBOX.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("id") and r["id"] not in done:
                r["parsed"] = parse_reply(r.get("text") or "")
                rows.append(r)
    return rows


def mark_done(email_id: str, result: str = "") -> None:
    """Mark one mail processed, and -- fk#1129 slice 3 -- record what happened to it, so a
    later reader (the morning brief's "Came in yesterday" section) can say more than a count.
    `result` is a short plain-language line: "board item #N created", "comment on #N",
    "answered ask #N", "steering issue #N", "dropped: <why>", or "" when nothing happened
    (kept, no action -- read as "pending" by came_in()). DONE keeps its old bare-id-per-line
    shape untouched; the result goes in a separate file so pending()'s `.split()` dedupe never
    sees it."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(DONE, "a") as fh:
        fh.write(email_id + "\n")
    if result:
        # gh#1219: RESULTS can be reassigned independently of LOG_DIR (tests patch inbox.RESULTS
        # directly, per test_inbox_came_in.py) or simply live under a LOG_DIR that no longer
        # matches the module-level default -- mkdir its OWN parent, don't assume LOG_DIR's mkdir
        # above covers it. On a fresh Linux CI runner with no prior ~/Library/Logs/fleet-kit,
        # relying on LOG_DIR's mkdir alone left RESULTS' real parent dir missing and this raised
        # FileNotFoundError inside every caller, including the HTTP handler thread in
        # webhook_receiver.py (which then looked like a RemoteDisconnected to the client).
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        with open(RESULTS, "a") as fh:
            fh.write(json.dumps({"id": email_id, "result": result, "at": time.time()}) + "\n")


def _result_bucket(result: str) -> str:
    """One result line -> the phrase it rolls up under: "board item #12 created" and "board
    item #12 created" both roll up as "board item"; "comment on #12" as "comment"; "answered
    ask #3" as "answered ask"; "steering issue #9" as "steering issue"; "dropped: ..." as
    "dropped"; anything else (including "pending") passes through as itself."""
    if result.startswith("board item"):
        return "board item"
    if result.startswith("comment on"):
        return "comment"
    if result.startswith("answered ask"):
        return "answered ask"
    if result.startswith("steering issue"):
        return "steering issue"
    if result.startswith("dropped"):
        return "dropped"
    return result


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def came_in(since_ts: float) -> dict:
    """fk#1129 slice 3: every intake row received since `since_ts`, grouped by `kind`, for the
    morning brief's "Came in yesterday" section. Returns:
      counts   -- {kind: n}, total rows per kind
      summary  -- {kind: "1 board item, 2 comments"}, the bucketed-result rollup per kind
      lines    -- {kind: [line, ...]}, one raw result line per row (mark_done()'s `result`,
                  or "pending" for a row not yet applied / applied with nothing to report)
    Rows missing `kind` (pre-fk#1129 rows written before classification existed) are read as
    "unknown" rather than dropped, so an old row does not silently vanish from the count."""
    results: dict[str, str] = {}
    if RESULTS.exists():
        for line in RESULTS.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("id"):
                results[r["id"]] = r.get("result") or "pending"
    counts: dict[str, int] = {}
    lines: dict[str, list[str]] = {}
    if INBOX.exists():
        for line in INBOX.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(r.get("received_at") or 0) < since_ts:
                continue
            kind = r.get("kind") or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
            lines.setdefault(kind, []).append(results.get(r.get("id"), "pending"))
    summary: dict[str, str] = {}
    for kind, kind_lines in lines.items():
        buckets: dict[str, int] = {}
        for result in kind_lines:
            b = _result_bucket(result)
            buckets[b] = buckets.get(b, 0) + 1
        summary[kind] = ", ".join(_plural(n, b) for b, n in buckets.items())
    dropped_total = sum(1 for kind_lines in lines.values() for result in kind_lines
                        if _result_bucket(result) == "dropped")
    return {"counts": counts, "summary": summary, "lines": lines, "dropped_total": dropped_total}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    d = sub.add_parser("done"); d.add_argument("id")
    p = sub.add_parser("parse"); p.add_argument("file")
    ap_ = sub.add_parser("apply"); ap_.add_argument("id")
    sub.add_parser("resolve", help="reply 'Resolved' on every filed thread whose issue has closed (fk#1106)")
    ci = sub.add_parser("came-in", help="counts + what happened, by kind, since N hours ago (fk#1129 slice 3)")
    ci.add_argument("--since-hours", type=float, default=24)
    a = ap.parse_args(argv)
    if a.cmd == "resolve":
        for line in resolve():
            print(line)
        return 0
    if a.cmd == "came-in":
        json.dump(came_in(time.time() - a.since_hours * 3600), sys.stdout, indent=1); print(); return 0
    if a.cmd == "apply":
        rows = [r for r in pending() if r["id"] == a.id]
        if not rows:
            print(f"no pending reply {a.id}", file=sys.stderr); return 1
        json.dump(apply(rows[0]), sys.stdout, indent=1); print(); return 0
    if a.cmd == "pending":
        json.dump(pending(), sys.stdout, indent=1); print(); return 0
    if a.cmd == "done":
        mark_done(a.id); print("done"); return 0
    json.dump(parse_reply(pathlib.Path(a.file).read_text()), sys.stdout, indent=1); print(); return 0


if __name__ == "__main__":
    sys.exit(main())
