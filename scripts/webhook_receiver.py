#!/usr/bin/env python3
"""webhook_receiver.py -- GitHub webhook -> run_member.sh, event-driven instead of polling.

Reif, 2026-08-21: the-fixer polls every 2 minutes, but a CI/deploy failure is a GitHub EVENT --
"a failed test" should fire the-fixer directly, not wait up to 2 minutes for the next poll.
This is the receiving end: a tiny stdlib HTTP server (no framework, matches member_spec.py's
own "whatever this needs to run on must already have it" reasoning) that verifies GitHub's
HMAC signature, checks the event is a completed+failed workflow_run on CI or deploy, and fires
`run_member.sh the-fixer` in the background.

WHY EVENT-DRIVEN DOESN'T REPLACE THE POLL: prod-down (health check failing with no CI signal
at all -- the exact case that motivated check.sh's double-probe) has no GitHub event to hook.
the-fixer's own poll interval should widen to a coarse backstop for THAT case once this is
wired in; this script only handles the two cases that DO have a real GitHub event: a red CI
run on the default branch, a red deploy run.

SECURITY: every request must carry a valid HMAC-SHA256 signature (X-Hub-Signature-256) keyed
on FLEET_WEBHOOK_SECRET, verified with a constant-time compare -- same shape as GitHub's own
docs recommend, this is the one place forging a request would let an attacker spend fleet
budget or spam the-fixer, so it is not optional.

Also handles "pull_request" events (opened/synchronize/reopened/ready_for_review, non-draft
only) -- judge-judy otherwise waits up to its own 15-minute poll to notice a fresh PR or a new
push to one; this fires it immediately instead. Safe to fire repeatedly on rapid pushes:
judge-judy's own checklist only reviews a head with no fresh fleet-code-review status, so a
redundant trigger is a fast no-op pass, never a double review.

Also serves three per-caller-token routes (fk#1124/fk#1129), auth'd by webhook_auth.caller_for
against FLEET_WEBHOOK_TOKENS -- neither the GitHub HMAC secret above nor the inbox route's
Svix secret, its own scheme, so a canary or a CI job never needs a GitHub-shaped credential:
  POST /webhook/intake  -- one central input dump (fk#1129 slice 2): any non-email input
                           (alert/monitoring/github/forward/steering) lands in the SAME
                           inbox.jsonl store as /webhook/inbox, source=<caller>, then the
                           same inbox.py apply() triage runs.
  POST /webhook/prod-alert -- a prod box's START/RESOLVE for one alert signature, deduped on
                           idempotency_key, appended to prod-alerts.jsonl for Reif HQ.
  POST /webhook/run     -- fire one fleet member now, off-cron, with the caller's own
                           credential (fk#1124) -- same Popen path /api/run_now uses, factored
                           into one function so there is ONE spawn path, not two.

Usage: FLEET_WEBHOOK_SECRET=<shared secret> FLEET_REPO=/path/to/target/repo \
         python3 webhook_receiver.py [--port 8562]
Wire GitHub -> Settings -> Webhooks -> Add webhook, Payload URL = this server's public path
(behind the Cloudflare Tunnel path ingress, e.g. https://dino.luckymachines.co/webhook),
Content type = application/json, Secret = the same FLEET_WEBHOOK_SECRET, events = "Workflow
runs" and "Pull requests".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
LOG_FILE = LOG_DIR / "webhook_receiver.log"
MERGED_PRS_FILE = LOG_DIR / "merged-prs.jsonl"
PROD_ALERTS_FILE = LOG_DIR / "prod-alerts.jsonl"
SECRET = os.environ.get("FLEET_WEBHOOK_SECRET", "")
# Which workflows count as a "the-fixer should look at this" failure. Space-separated
# filenames, matched against workflow_run.path's basename -- same env-var convention as
# check.sh's FIXER_CI_WORKFLOW/FIXER_DEPLOY_WORKFLOW, so operators configure both once.
WATCHED_WORKFLOWS = set(
    os.environ.get("FLEET_WEBHOOK_WORKFLOWS", "ci.yml deploy.yml").split()
)
# judge-judy otherwise waits up to its own 15-minute poll interval to notice a brand-new or
# freshly-pushed PR -- opened/synchronize are real GitHub events same as workflow_run, so fire
# it immediately instead of leaving a fresh PR sitting unreviewed for up to a quarter hour.
PR_TRIGGER_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # gh#1219: LOG_FILE is derived from LOG_DIR once at import time, so a caller (or test) that
    # reassigns module-level LOG_DIR afterward leaves LOG_FILE pointing at the old path -- mkdir
    # ITS parent too, don't assume the LOG_DIR mkdir above covers it. On a fresh Linux CI runner
    # this raised FileNotFoundError inside the HTTP handler thread, which the client only ever
    # saw as a RemoteDisconnected.
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    import datetime

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    with LOG_FILE.open("a") as fh:
        fh.write(f"[{ts}] {msg}\n")


def verify_signature(body: bytes, signature_header: str) -> bool:
    if not SECRET:
        return False  # never accept unsigned/unconfigured -- fail closed, not open
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    got = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, got)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence BaseHTTPServer's default stderr chatter
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = self.path.rstrip("/")
        if path.endswith("/inbox"):
            self._handle_inbox(body)
            return
        if path.endswith("/webhook/intake"):
            self._handle_intake(body)
            return
        if path.endswith("/webhook/run"):
            self._handle_run(body)
            return
        if path.endswith("/webhook/prod-alert"):
            self._handle_prod_alert(body)
            return
        sig = self.headers.get("X-Hub-Signature-256", "")

        if not verify_signature(body, sig):
            log(f"REJECTED: bad or missing signature from {self.client_address[0]}")
            self.send_response(401)
            self.end_headers()
            return

        event = self.headers.get("X-GitHub-Event", "")
        # GitHub's webhook config offers "application/json" vs "application/x-www-form-urlencoded"
        # content types, and this kit's setup always requests json -- but a real delivery was
        # observed arriving form-urlencoded anyway (config.content_type correctly saved as
        # "application/json" on GitHub's side, Content-Type header on the actual POST said
        # x-www-form-urlencoded regardless). Unwrap defensively rather than trust the config
        # matches the wire format: form-encoded wraps the JSON as one field named `payload`.
        ctype = self.headers.get("Content-Type", "")
        raw_json = body
        if "x-www-form-urlencoded" in ctype:
            from urllib.parse import parse_qs

            parsed = parse_qs(body.decode("utf-8", errors="replace"))
            raw_json = (parsed.get("payload") or [""])[0].encode("utf-8")
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            log(f"REJECTED: unparseable body (content-type={ctype!r})")
            self.send_response(400)
            self.end_headers()
            return

        if event == "ping":
            log("ping received -- webhook configured correctly")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
            return

        if event == "pull_request":
            self._handle_pull_request(payload)
            return

        if event != "workflow_run":
            self.send_response(204)
            self.end_headers()
            return

        wr = payload.get("workflow_run", {})
        status = wr.get("status")
        conclusion = wr.get("conclusion")
        wf_path = wr.get("path", "")
        wf_name = Path(wf_path).name if wf_path else ""

        self.send_response(200)  # ack immediately -- GitHub retries on non-2xx, don't hold it
        self.end_headers()

        if status != "completed":
            return
        if wf_name not in WATCHED_WORKFLOWS:
            log(f"ignored: workflow_run for {wf_name!r}, not in watch list {WATCHED_WORKFLOWS}")
            return
        if conclusion != "failure":
            log(f"ignored: {wf_name} completed with conclusion={conclusion}, not a fire")
            return

        sha = wr.get("head_sha", "?")[:12]
        fire, why = should_fire(payload)
        if not fire:
            log(f"ignored: {wf_name} failed at {sha} -- {why}")
            return
        log(f"FIRE: {wf_name} failed at {sha} -- launching the-fixer")
        _launch_member("the-fixer")

    def _handle_inbox(self, body: bytes) -> None:
        """fk#669: Resend's email.received webhook -- Reif replied to a brief. Svix signature
        (a different scheme from GitHub's), sender allowlist, fetch the full message, store it,
        kick the messenger's inbox pass. Never 5xx on a bad message: Resend would retry forever."""
        import inbox as inbox_mod
        secret = env_value("FLEET_INBOX_WEBHOOK_SECRET")
        if not inbox_mod.verify_svix(body, dict(self.headers.items()), secret):
            log(f"inbox REJECTED: bad or missing svix signature from {self.client_address[0]}")
            self.send_response(401); self.end_headers(); return
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400); self.end_headers(); return
        if event.get("type") != "email.received":
            self.send_response(204); self.end_headers(); return
        data = event.get("data") or {}
        sender = data.get("from") or ""
        # Support-inbox mail (in.philanthropy.org, prod's) is not for the fleet: drop it before
        # the trusted/budget decision and before anything is stored. Checked on the webhook data
        # first (no fetch needed), then again on the fetched message, whose received_for carries
        # the envelope recipient a BCC'd copy only shows there.
        ignore = env_value("FLEET_INBOX_IGNORE_TO_DOMAINS") or inbox_mod.IGNORE_TO_DOMAINS_DEFAULT
        if inbox_mod.ignored_recipient(data, ignore):
            log(f"inbox IGNORED: addressed to {ignore}")
            self.send_response(200); self.end_headers(); self.wfile.write(b"ignored"); return
        try:
            email = inbox_mod.fetch_received(data.get("email_id") or "")
        except Exception as exc:  # noqa: BLE001
            log(f"inbox: fetch of {data.get('email_id')} failed: {exc} -- storing metadata only")
            email = None
        if email is not None and inbox_mod.ignored_recipient(data, ignore, email):
            log(f"inbox IGNORED: addressed to {ignore}")
            self.send_response(200); self.end_headers(); self.wfile.write(b"ignored"); return
        if email is None:
            email = {"id": data.get("email_id"), "from": sender, "subject": data.get("subject"), "text": ""}
        trusted = inbox_mod.allowed_sender(sender, env_value("FLEET_INBOX_FROM"))
        # Reif 2026-09-16: "Make it open - fleet can decide if something is garbage or not."
        # An unknown sender is stored UNTRUSTED: it can never answer an ask or auto-file (that
        # would let anyone on the internet steer the fleet by email); the messenger reads it and
        # files or bins it. Capped per hour so a spam burst cannot fill the inbox.
        if not trusted and not inbox_mod.untrusted_budget_ok():
            log(f"inbox DROPPED: untrusted sender {sender!r} over the hourly cap")
            self.send_response(200); self.end_headers(); self.wfile.write(b"dropped"); return
        row = inbox_mod.store(email, data, trusted=trusted)
        # fk#1056: ask answers and `backlog:` mails need no model; do them here, reply, and
        # only hand leftover free text to the messenger. A failure inside apply() must not
        # lose the mail: it stays pending and the messenger pass reads it as before.
        try:
            applied = inbox_mod.apply(row)
        except Exception as exc:  # noqa: BLE001
            log(f"inbox: apply of {row.get('id')} failed: {exc} -- messenger takes it")
            applied = {"done": False}
        if not applied.get("done"):
            _launch_member("dont-shoot-the-messenger", ["--task", "inbox"])
        self.send_response(200); self.end_headers(); self.wfile.write(b"stored")

    def _handle_pull_request(self, payload: dict) -> None:
        self.send_response(200)  # ack immediately, same reasoning as the workflow_run path
        self.end_headers()

        action = payload.get("action", "")
        pr = payload.get("pull_request", {})

        if action == "closed" and pr.get("merged"):
            record_merged_pr(payload)

        if pr.get("draft"):
            log(f"ignored: pull_request {action} on draft PR #{pr.get('number', '?')}")
            return
        if action not in PR_TRIGGER_ACTIONS:
            log(f"ignored: pull_request action={action!r}, not in {PR_TRIGGER_ACTIONS}")
            return

        num = pr.get("number", "?")
        log(f"FIRE: pull_request {action} on PR #{num} -- launching judge-judy")
        _launch_member("judge-judy")

    def _handle_intake(self, body: bytes) -> None:
        """fk#1129 slice 2: POST /webhook/intake -- the central input dump's non-email side.
        Same store as /webhook/inbox (inbox.py's LOG_DIR/inbox.jsonl), same triage
        (inbox.py apply()), but for a caller that isn't an email at all: a canary, a CI red-
        on-main detector, DigitalOcean monitoring hitting this directly rather than through
        Resend. Body: {"kind": "alert|monitoring|github|forward|steering", "source": "<free
        text>", "subject": "...", "body": "...", "check": "<optional>"}. kind is trusted from
        the caller (already authenticated by its own token) rather than re-classified from a
        from/subject shape that doesn't exist here; missing kind falls back to classify()."""
        import inbox as inbox_mod
        caller = self._caller()
        if not caller:
            log(f"intake REJECTED: bad or missing token from {self.client_address[0]}")
            self.send_response(401); self.end_headers(); return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400); self.end_headers(); return
        if not isinstance(payload, dict):
            self.send_response(400); self.end_headers(); return
        subject = payload.get("subject") or ""
        text = payload.get("body") or ""
        mail = {"from": caller, "subject": subject, "text": text}
        kind = payload.get("kind") or inbox_mod.classify(mail)
        row = {
            "id": f"intake-{caller}-{int(time.time() * 1000)}-{os.urandom(3).hex()}",
            "received_at": time.time(),
            "trusted": False,  # fk#1129: a webhook caller is never in FLEET_INBOX_FROM's
                               # steering set -- it can land an alert/backlog item through the
                               # fixed machine kinds below, never answer an ask or free-text steer.
            "from": caller,
            "subject": subject,
            "text": text,
            "full_text": text[:20000],
            "kind": kind,
            "source": payload.get("source") or caller,
        }
        check = payload.get("check") or inbox_mod.alert_check(subject)
        if check:
            row["check"] = check
        inbox_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(inbox_mod.INBOX, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        log(f"intake stored {row['id']} from caller={caller} kind={kind} source={row['source']!r}")
        try:
            applied = inbox_mod.apply(row)
        except Exception as exc:  # noqa: BLE001
            log(f"intake: apply of {row['id']} failed: {exc} -- messenger takes it")
            applied = {"done": False}
        if not applied.get("done"):
            _launch_member("dont-shoot-the-messenger", ["--task", "inbox"])
        self.send_response(200); self.end_headers(); self.wfile.write(b"stored")

    def _handle_prod_alert(self, body: bytes) -> None:
        """POST /webhook/prod-alert -- a prod box's START/RESOLVE transition for one alert
        signature (nonprofit-atlas scripts/box/hq_alert.py). Same per-caller bearer tokens as
        /webhook/intake. Deduped on idempotency_key (the sender retries an unacked push with the
        same key), then appended to prod-alerts.jsonl, which notify_hq_prod_alert.py (host side,
        systemd .path) hands to Reif HQ as a headless job."""
        caller = self._caller()
        if not caller:
            log(f"prod-alert REJECTED: bad or missing token from {self.client_address[0]}")
            self.send_response(401); self.end_headers(); return
        try:
            record = prod_alert_record(json.loads(body), caller)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"ok": False, "error": str(exc)[:200]}); return
        fresh = record_prod_alert(record)
        log(f"prod-alert {'recorded' if fresh else 'duplicate'}: {record['transition'].upper()} "
            f"{record['signature']} caller={caller} key={record['idempotency_key']}")
        self._json(200, {"ok": True, "duplicate": not fresh})

    def _caller(self) -> str | None:
        import webhook_auth
        return webhook_auth.caller_for(self.headers.get("Authorization"),
                                       env_value("FLEET_WEBHOOK_TOKENS") or None)

    def _handle_run(self, body: bytes) -> None:
        """fk#1124: POST /webhook/run -- fire one fleet member now, with the caller's own
        token (never the dashboard's FLEET_API_KEY). {"member": "<name>", "item": <n
        optional>, "reason": "<one line>"}. Same Popen path /api/run_now uses
        (_run_member_bg, factored so there is one spawn path); the run record carries
        fired_by=<caller> and reason. One in-flight run per member: a second call while the
        first is still running gets 409 with the running run_id. Disabled members still fire
        -- an explicit webhook call is a human/agent decision, same as FLEET_RUN_NOW=1 today."""
        caller = self._caller()
        if not caller:
            log(f"run REJECTED: bad or missing token from {self.client_address[0]}")
            self.send_response(401); self.end_headers(); return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400); self.end_headers(); return
        if not isinstance(payload, dict):
            self.send_response(400); self.end_headers(); return
        member = str(payload.get("member") or "").strip()
        if not member:
            self._json(400, {"ok": False, "error": "member is required"}); return
        try:
            import member_spec
            if member != "judge-judy":  # judge-judy runs its own script, not run_member.sh
                member_spec.by_name(member)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"unknown member {member!r}: {exc}"}); return
        item = payload.get("item")
        if item is not None and (not isinstance(item, int) or isinstance(item, bool)):
            self._json(400, {"ok": False, "error": "item must be an integer issue number"}); return
        reason = str(payload.get("reason") or "").strip()

        running = _running_run_id(member)
        if running:
            log(f"run REJECTED: {member} already running ({running}), caller={caller}")
            self._json(409, {"ok": False, "error": "already running", "run_id": running}); return

        log(f"FIRE: /webhook/run member={member} item={item} caller={caller} reason={reason!r}")
        import member_launch
        member_launch.spawn(member, item=item, fired_by=caller, reason=reason)
        self._json(200, {"ok": True, "started": member})

    def _json(self, code: int, obj: dict) -> None:
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def should_fire(payload: dict) -> tuple[bool, str]:
    """fk#1055: a red run is an incident only on the default branch. Every PR branch and
    merge-queue batch that fails CI used to launch the-fixer too -- measured 2026-09-16 on
    dino: ~20 FIRE lines an hour, 283 the-fixer runs in 6h, 114 of them dispatch_skipped and
    61 quiet, each a paid LLM pass that ran check.sh and found nothing to fight. A PR's red
    CI is the PR's problem (judge-judy, its author); main's red CI is the fleet's."""
    wr = payload.get("workflow_run") or {}
    branch = wr.get("head_branch") or ""
    default = (payload.get("repository") or {}).get("default_branch") or "main"
    if branch == default:
        return True, "default branch"
    return False, f"head_branch {branch!r} is not the default branch {default!r}"


CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE
)


def closes_refs(body: str) -> list[int]:
    """'Closes #N' / 'Fixes #N' / 'Resolves #N' (any tense, GitHub's own auto-close set),
    deduped and in first-seen order -- a PR body commonly repeats the same ref."""
    seen: list[int] = []
    for m in CLOSES_RE.finditer(body or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def merged_pr_record(payload: dict) -> dict:
    """The one JSON line appended to logs/merged-prs.jsonl per merged PR -- Reif HQ's webhook
    task: number, title, url, merged_at, author, and any 'Closes #N' refs from the body."""
    pr = payload.get("pull_request") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "url": pr.get("html_url") or "",
        "merged_at": pr.get("merged_at"),
        "author": (pr.get("user") or {}).get("login") or "",
        "closes": closes_refs(pr.get("body") or ""),
    }


def record_merged_pr(payload: dict) -> None:
    """Append-only, one line per merge -- only the default branch counts as a fleet-kit PR
    Reif's chat cares about; a merge into a feature/release branch is not 'done'."""
    pr = payload.get("pull_request") or {}
    base = (pr.get("base") or {}).get("ref") or ""
    default = (payload.get("repository") or {}).get("default_branch") or "main"
    if base != default:
        log(f"merged-pr ignored: PR #{pr.get('number', '?')} merged into {base!r}, not default {default!r}")
        return
    record = merged_pr_record(payload)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(MERGED_PRS_FILE, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    log(f"merged-pr recorded: #{record['number']} {record['title']!r}")


PROD_ALERT_TRANSITIONS = {"start", "resolve"}


def prod_alert_record(payload, caller: str) -> dict:
    """Validated, size-clipped JSON line for prod-alerts.jsonl. Raises ValueError on a bad
    shape -- this text ends up in an HQ prompt, so only known fields, bounded lengths."""
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    sig = str(payload.get("signature") or "").strip()
    transition = str(payload.get("transition") or "").strip()
    key = str(payload.get("idempotency_key") or "").strip()
    if not sig or len(sig) > 200:
        raise ValueError("signature required (<=200 chars)")
    if transition not in PROD_ALERT_TRANSITIONS:
        raise ValueError(f"transition must be one of {sorted(PROD_ALERT_TRANSITIONS)}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        raise ValueError("idempotency_key required ([A-Za-z0-9_-]{8,128})")

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "received_at": int(time.time()),
        "caller": caller,
        "host": str(payload.get("host") or "")[:100],
        "signature": sig,
        "transition": transition,
        "start_ts": _int(payload.get("start_ts")),
        "observed_ts": _int(payload.get("observed_ts")),
        "title": str(payload.get("title") or "")[:200],
        "detail": str(payload.get("detail") or "")[:500],
        "idempotency_key": key,
    }


def record_prod_alert(record: dict) -> bool:
    """Append unless this idempotency_key is already in the file. True = new. Locked, so two
    concurrent retries of the same push cannot both pass the check."""
    import fcntl

    PROD_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROD_ALERTS_FILE, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        needle = f'"idempotency_key": "{record["idempotency_key"]}"'
        if any(needle in line for line in fh):
            return False
        fh.write(json.dumps(record) + "\n")
    return True


def env_value(key: str) -> str:
    """Process env first, then the instance's fleet.env -- this process is started by
    entrypoint.sh with only the vars it was handed, and the inbox secret lives in fleet.env."""
    if os.environ.get(key):
        return os.environ[key]
    path = os.environ.get("FLEET_ENV_FILE", "/fleet-kit/fleet.env")
    try:
        for line in open(path, errors="ignore"):
            m = re.match(rf"^\s*(?:export\s+)?{key}\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip("\"'")
    except OSError:
        pass
    return ""


def _running_run_id(member: str) -> str | None:
    """fk#1124: the run_id of `member`'s in-flight run, or None. A run is in-flight when its
    most recent runs.jsonl row is a `started` row (run_report.py's build_started_record,
    written by run_member.sh before `claude -p` even runs -- see that module's header) with no
    completion row (any other status, sharing the same run_id) after it -- same pairing
    fleet_stats.lost_passes() uses to detect a run that vanished, reused here with zero grace
    instead of that function's 90-minute default, because this call needs 'is it running right
    now', not 'has it been gone long enough to call a gap'."""
    import fleet_stats
    rows = []
    try:
        with open(LOG_DIR / "runs.jsonl", errors="ignore") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    lost = fleet_stats.lost_passes(rows, grace_minutes=0.0)
    for r in lost:
        if r.get("member") == member:
            return r.get("run_id")
    return None


def _launch_member(name: str, args: list[str] | None = None) -> None:
    run_member = KIT_DIR / "scripts" / "run_member.sh"
    try:
        # Detached, best-effort: this receiver's job is to notice and hand off, not to wait
        # out a review/incident-response pass (which can run up to that member's own
        # timeout_s). A failure to LAUNCH is logged; the member's own log covers the rest.
        subprocess.Popen(
            [str(run_member), name, *(args or [])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        log(f"FATAL: could not launch run_member.sh {name}: {e}")


def main() -> int:
    if not SECRET:
        print(
            "FATAL: FLEET_WEBHOOK_SECRET not set -- refusing to start unsigned (webhook_receiver.py "
            "would accept nothing, which is safe, but that's not the same as a useful server). "
            "Set it to the same value configured in GitHub's webhook settings.",
            file=sys.stderr,
        )
        return 2
    # FLEET_WEBHOOK_PORT is what fleet.env/deploy.sh already use to place this port; reading it
    # here means the receiver lands where the rest of the kit expects without every caller
    # having to remember --port. Explicit --port still wins, for a one-off on a busy box.
    port = int(os.environ.get("FLEET_WEBHOOK_PORT") or 8562)
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    log(f"listening on :{port}, watching {WATCHED_WORKFLOWS}")
    # 0.0.0.0, not 127.0.0.1: inside a container, podman's port-forward connects from OUTSIDE
    # the container's own loopback -- a receiver bound to 127.0.0.1 is unreachable through
    # `-p PORT:PORT` even though `podman exec ... curl localhost` finds it fine. Same reasoning
    # as fleet_view_server.py's own bind (see that file). Found live: this exact bug produced a
    # 502 through the Cloudflare Tunnel while the receiver tested healthy from inside its own
    # container namespace the whole time.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
