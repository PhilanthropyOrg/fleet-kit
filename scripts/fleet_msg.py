#!/usr/bin/env python3
"""fleet_msg -- member-to-member messages, acked or escalated (philanthropy#8215).

THE GAP THIS CLOSES. Reif, 2026-09-26: "I'm talking about internal messaging, not to me, but to
each other -- if there are 300+ issues and only a few with a quality label, shouldn't both jefe
and marie get a notice?" Every message path in the kit pointed at Reif (run_mail.py,
messenger_brief, ask.py, inbox.py). No member could tell another member anything: gru's gate
drops, a sandbox denial, an idle PR, a failing prod journey all reached a log or a human, never
the member whose job it was to act.

THE BUS. One `msgs` table in fleet.db (next to `asks`, same file, same connect()):

  send      one row per recipient; a (to, kind, key) already sent in the last 6h is not re-sent.
            a WAKE_KINDS kind (cause/incident/pr-block/nudge) also launches the recipient's next
            pass now, detached like dispatch_member.sh, coalesced to one per
            FLEET_MSG_WAKE_COOLDOWN_S -- marie having 7 unread at her next 4h cron tick is the
            gap this closes
  inbox     a member's open messages. run_member.sh puts `inbox --render` FIRST in every pass's
            prompt, so a member reads its mail before its charter
  ack       "I did it" (status acked) -- or `reply`, "I didn't, and here is why" (status
            replied). Both close the message; the note is the record
  watchdog  a message open for 2 of its recipient's cadences escalates to jefe (a new message
            pointing at the old one); one jefe leaves open for 2 of ITS cadences becomes ONE
            ask.py ask for Reif, however many pile up
  summary   open by member + the oldest unacked (the console tile)
  pregate   `green` when a member's inbox is empty -- jefe's llm.pregate, so an empty desk
            costs $0

Escalation never closes the original: the recipient can still ack it, and jefe's ack of an
escalation closes the message it points at too.

Usage:
  fleet_msg.py send --from gru --to marie,jefe --kind gate-drop --key gate-drops --body TEXT [--items 1,2]
  fleet_msg.py inbox --me marie [--render] [--mark-read] [--json]   (--me hq: HQ on the host)
  fleet_msg.py ack   --me marie --id 12 --note "labeled #1 #2 quality:solid"
  fleet_msg.py reply --me marie --id 12 --reason "#3 is an epic; nothing to label"
  fleet_msg.py watchdog [--dry-run]
  fleet_msg.py summary
  fleet_msg.py pregate --me jefe
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402

DEDUPE_S = float(os.environ.get("FLEET_MSG_DEDUPE_S", 6 * 3600))
ESCALATE_AFTER_CADENCES = 2
DEFAULT_CADENCE_S = 3600
JEFE = "jefe"
# Recipients with no members/<name>/ spec. `hq` is the Claude session on the host (not in the
# container) that ticks every 30 min, merges finished checkpoint drafts and takes
# fleet:needs-prod-access items (philanthropy#8218). It reads with `inbox --me hq`.
HOST_MEMBERS_CADENCE_S = {"hq": 1800.0}
REIF_TAG = "unacked member messages:"
MSG_COLUMNS = ("id", "sender", "recipient", "kind", "key", "body", "items", "status", "sent_at",
               "read_at", "acked_at", "ack_note", "escalated_to", "escalated_at", "parent_id",
               "ask_id")


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        try:
            r["items"] = json.loads(r.get("items") or "[]")
        except ValueError:
            r["items"] = []
        out.append(r)
    return out


def send(conn, sender: str, recipients, kind: str, key: str, body: str,
         items=None, parent_id: int | None = None, now: float | None = None) -> list[dict]:
    """One row per recipient. Returns [{"to", "id"|None, "deduped": bool}]."""
    now = time.time() if now is None else now
    out = []
    for to in [r.strip() for r in (recipients if isinstance(recipients, (list, tuple))
                                   else str(recipients).split(",")) if r.strip()]:
        dup = conn.execute(
            "SELECT id FROM msgs WHERE recipient = ? AND kind = ? AND key = ? AND sent_at > ? "
            "ORDER BY id DESC LIMIT 1", (to, kind, key, now - DEDUPE_S)).fetchone()
        if dup:
            out.append({"to": to, "id": dup[0], "deduped": True})
            continue
        cur = conn.execute(
            "INSERT INTO msgs (sender, recipient, kind, key, body, items, status, sent_at, "
            "parent_id) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (sender, to, kind, key, body, json.dumps(list(items or [])), now, parent_id))
        out.append({"to": to, "id": cur.lastrowid, "deduped": False})
    conn.commit()
    return out


# --- wake ------------------------------------------------------------------------------------
# THE GAP: cron cadence is the ONLY thing that reads a message before this -- marie runs every
# 4h and had 7 unread, nerd sat on one for 7h, dumbledore's `cause` msg landed 30min after his
# pass had already ended for the cadence. A WAKE_KINDS kind launches the recipient's next pass
# now instead of waiting. `send()` itself stays pure (existing tests call it directly); this
# runs after it, from the CLI `send` path only.

WAKE_KINDS = {k.strip() for k in os.environ.get(
    "FLEET_MSG_WAKE_KINDS", "cause,incident,pr-block,nudge").split(",") if k.strip()}
WAKE_COOLDOWN_S = float(os.environ.get("FLEET_MSG_WAKE_COOLDOWN_S", 1800))


def _log_dir(log_dir=None) -> Path:
    return Path(log_dir or os.environ.get(
        "FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()


def _wake_stamp_path(member: str, log_dir=None) -> Path:
    return _log_dir(log_dir) / "wake" / f"{member}.last"


def _cooling_down(member: str, now: float, cooldown_s: float, log_dir=None) -> bool:
    try:
        last = float(_wake_stamp_path(member, log_dir).read_text().strip())
    except (OSError, ValueError):
        return False
    return (now - last) < cooldown_s


def _stamp_wake(member: str, now: float, log_dir=None) -> None:
    p = _wake_stamp_path(member, log_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(now))


def _default_launcher(member: str, reason: str) -> None:
    """setsid nohup detached run_member.sh <member>, same shape as dispatch_member.sh's own
    launch, appending to $FLEET_LOG_DIR/<member>.log. FLEET_RUN_MEMBER overrides the script
    path -- same env name dispatch_member.sh reads, so a test can point it at a stub instead of
    spawning a real pass. No FLEET_RUN_NOW=1: run_member.sh:309 uses that to bypass
    enabled=false, and disabled members are already filtered out before a launcher is ever
    called."""
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_member = os.environ.get("FLEET_RUN_MEMBER", str(HERE / "run_member.sh"))
    env = dict(os.environ)
    env["FLEET_FIRED_BY"] = "fleet_msg"
    env["FLEET_FIRED_REASON"] = reason
    if not env.get("GH_TOKEN"):
        tok_path = Path("/root/.gh_token")
        try:
            if tok_path.exists():
                env["GH_TOKEN"] = tok_path.read_text().strip()
        except OSError:
            pass
    with open(log_dir / f"{member}.log", "a") as log_f:
        subprocess.Popen(["setsid", "nohup", "bash", run_member, member], stdin=subprocess.DEVNULL,
                         stdout=log_f, stderr=log_f, env=env, start_new_session=True)


def wake(conn, sent: list[dict], kind: str, specs=None, now: float | None = None, launcher=None,
         cooldown_s: float | None = None, log_dir=None) -> list[dict]:
    """After send(): for each non-deduped row whose kind is in WAKE_KINDS, wake that recipient
    now -- unless it's hq, has no members/<name>/ spec, is enabled=false, or was already woken
    within cooldown_s. Returns `sent` with "woken" (bool) and "wake_reason" (why not, else
    None) added to each row."""
    now = time.time() if now is None else now
    cooldown_s = WAKE_COOLDOWN_S if cooldown_s is None else cooldown_s
    launcher = launcher or _default_launcher
    if specs is None:
        specs = _specs()
    out = []
    for row in sent:
        row = dict(row)
        to = row["to"]
        if row.get("deduped"):
            reason = "deduped"
        elif kind not in WAKE_KINDS:
            reason = "kind"
        elif to == "hq":
            reason = "hq"
        elif to not in specs:
            reason = "no-spec"
        elif not specs[to].get("enabled", True):
            reason = "disabled"
        elif _cooling_down(to, now, cooldown_s, log_dir):
            reason = "cooldown"
        else:
            reason = None
        if reason is None:
            launcher(to, f"msg-{row['id']}-{kind}")
            _stamp_wake(to, now, log_dir)
        row["woken"], row["wake_reason"] = reason is None, reason
        out.append(row)
    return out


def inbox(conn, me: str) -> list[dict]:
    return _rows(conn.execute(
        f"SELECT {', '.join(MSG_COLUMNS)} FROM msgs WHERE recipient = ? AND status = 'open' "
        "ORDER BY sent_at, id", (me,)))


def get(conn, msg_id: int) -> dict | None:
    rows = _rows(conn.execute(f"SELECT {', '.join(MSG_COLUMNS)} FROM msgs WHERE id = ?", (msg_id,)))
    return rows[0] if rows else None


def mark_read(conn, ids, now: float | None = None) -> None:
    now = time.time() if now is None else now
    conn.executemany("UPDATE msgs SET read_at = ? WHERE id = ? AND read_at IS NULL",
                     [(now, i) for i in ids])
    conn.commit()


def close(conn, me: str, msg_id: int, note: str, acted: bool = True,
          now: float | None = None) -> dict:
    """ack (acted) or reply (did not act, with the reason). Only the recipient may close its own
    message. Closing a jefe escalation closes the message it points at too."""
    now = time.time() if now is None else now
    note = (note or "").strip()
    if not note:
        raise ValueError("a note is required: what you did, or why not")
    m = get(conn, msg_id)
    if not m:
        raise ValueError(f"no message {msg_id}")
    if m["recipient"] != me:
        raise ValueError(f"message {msg_id} is to {m['recipient']}, not {me}")
    if m["status"] != "open":
        return m
    status = "acked" if acted else "replied"
    conn.execute("UPDATE msgs SET status = ?, acked_at = ?, ack_note = ?, "
                 "read_at = COALESCE(read_at, ?) WHERE id = ?", (status, now, note, now, msg_id))
    if m["parent_id"] and m["kind"] == "escalation":
        conn.execute("UPDATE msgs SET status = ?, acked_at = ?, ack_note = ? "
                     "WHERE id = ? AND status = 'open'",
                     (status, now, f"{me} (escalation #{msg_id}): {note}", m["parent_id"]))
    conn.commit()
    return get(conn, msg_id)


# --- cadence -------------------------------------------------------------------------------

def _hour_field_s(field: str) -> float | None:
    field = (field or "").strip()
    if field in ("", "*"):
        return 3600.0
    m = re.fullmatch(r"\*/(\d+)", field)
    if m:
        return int(m.group(1)) * 3600.0
    parts = [p for p in field.split(",") if p.strip().isdigit()]
    if len(parts) > 1:
        return 24 * 3600.0 / len(parts)
    return 24 * 3600.0 if parts else None


def cadence_s(member: str, specs: dict | None = None, env=os.environ) -> float:
    """Seconds between a member's scheduled passes: FLEET_MSG_CADENCE_S_<M> override, then the
    instance's hour-field knob (FLEET_GRU_CADENCE / FLEET_MARIE_CADENCE, what entrypoint.sh
    puts on cron), then the spec's schedule. A member with no known schedule gets an hour."""
    envname = member.upper().replace("-", "_")
    if env.get(f"FLEET_MSG_CADENCE_S_{envname}"):
        return float(env[f"FLEET_MSG_CADENCE_S_{envname}"])
    if env.get(f"FLEET_{envname}_CADENCE"):
        s = _hour_field_s(env[f"FLEET_{envname}_CADENCE"])
        if s:
            return s
    if member in HOST_MEMBERS_CADENCE_S:
        return HOST_MEMBERS_CADENCE_S[member]
    if specs is None:
        specs = _specs()
    sched = (specs.get(member) or {}).get("schedule") or {}
    if sched.get("interval_s"):
        return float(sched["interval_s"])
    if "hourly_at_minute" in sched:
        return 3600.0
    if sched.get("daily_at"):
        return 24 * 3600.0
    return float(DEFAULT_CADENCE_S)


def _specs() -> dict:
    try:
        import member_spec
        return {s["name"]: s for s in member_spec.load_all()}
    except Exception:  # noqa: BLE001 -- a broken spec must not stop the watchdog
        return {}


# --- watchdog ------------------------------------------------------------------------------

def watchdog_plan(open_msgs: list[dict], cadence, now: float) -> dict:
    """Pure. Which open messages escalate to jefe, and which (jefe's own) go to Reif."""
    to_jefe, to_reif = [], []
    for m in open_msgs:
        if m.get("escalated_to"):
            continue
        if now - float(m["sent_at"]) <= ESCALATE_AFTER_CADENCES * cadence(m["recipient"]):
            continue
        (to_reif if m["recipient"] == JEFE else to_jefe).append(m)
    return {"to_jefe": to_jefe, "to_reif": to_reif}


def _age(s: float) -> str:
    return f"{s / 3600:.1f}h" if s >= 3600 else f"{int(s // 60)}m"


def watchdog(conn, now: float | None = None, cadence=None, dry_run: bool = False,
             file_ask=None) -> dict:
    now = time.time() if now is None else now
    if cadence is None:
        specs = _specs()
        cadence = lambda m: cadence_s(m, specs)  # noqa: E731
    open_msgs = _rows(conn.execute(
        f"SELECT {', '.join(MSG_COLUMNS)} FROM msgs WHERE status = 'open' ORDER BY sent_at"))
    p = watchdog_plan(open_msgs, cadence, now)
    out = {"escalated_to_jefe": [], "escalated_to_reif": [], "ask_id": None}
    if dry_run:
        out["escalated_to_jefe"] = [m["id"] for m in p["to_jefe"]]
        out["escalated_to_reif"] = [m["id"] for m in p["to_reif"]]
        return out
    for m in p["to_jefe"]:
        body = (f"{m['recipient']} has not acked message #{m['id']} ({m['kind']} from "
                f"{m['sender']}) after {_age(now - m['sent_at'])} -- 2 of its cadences. Get it "
                f"done (or get it answered) and ack this; acking closes #{m['id']} too.\n\n"
                f"Original:\n{m['body']}")
        send(conn, "watchdog", [JEFE], "escalation", f"msg:{m['id']}", body,
             items=m["items"], parent_id=m["id"], now=now)
        conn.execute("UPDATE msgs SET escalated_to = ?, escalated_at = ? WHERE id = ?",
                     (JEFE, now, m["id"]))
        out["escalated_to_jefe"].append(m["id"])
    if p["to_reif"]:
        import ask as ask_mod
        open_asks = [a for a in ask_mod.list_asks(conn, status="open", member=JEFE, limit=200)
                     if (a.get("why") or "").startswith(REIF_TAG)]
        ids = [m["id"] for m in p["to_reif"]]
        if open_asks:
            ask_id = open_asks[0]["id"]  # ONE ask: fold into the one Reif already has
        else:
            listed = "; ".join(f"#{m['id']} {m['kind']} ({_age(now - m['sent_at'])})"
                               for m in p["to_reif"])
            why = (f"{REIF_TAG} jefe left {len(ids)} escalated message(s) unacked for 2 of its "
                   f"cadences: {listed}. `fleet_msg.py inbox --me jefe` has them in full.")
            fa = file_ask or (lambda **kw: ask_mod.file_ask(conn, **kw))
            ask_id = fa(member=JEFE, why=why, unblocks="the members those messages were for",
                        proposed="look at jefe's inbox, or tell the fleet who owns it now",
                        ask_class="decision",
                        summary=f"{len(ids)} internal fleet message(s) went unanswered")
        conn.executemany("UPDATE msgs SET escalated_to = 'reif', escalated_at = ?, ask_id = ? "
                         "WHERE id = ?", [(now, ask_id, i) for i in ids])
        out["escalated_to_reif"] = ids
        out["ask_id"] = ask_id
    conn.commit()
    return out


# --- the-fixer -> owner: a PR idle > 4h --------------------------------------------------------

IDLE_PR_S = float(os.environ.get("FLEET_MSG_IDLE_PR_S", 4 * 3600))
# Fleet branches are `member/<name>-<rest>`. A minion is dispatched by gru and has no desk of
# its own, so gru owns its PRs; any other scheduled member owns its own. Everything else (a
# human, dependabot, an HQ session) is not a fleet member's to answer and gets no message.
def pr_owner(branch: str, members) -> str | None:
    if not (branch or "").startswith("member/"):
        return None
    rest = branch[len("member/"):]
    for name in sorted(set(members) | {"minion"}, key=len, reverse=True):
        if rest == name or rest.startswith(name + "-"):
            return "gru" if name == "minion" else name
    return None


def idle_pr_messages(prs: list[dict], members, now: float, idle_s: float = IDLE_PR_S) -> list[dict]:
    """Pure. One message per owner listing its idle PRs (no update in idle_s)."""
    import datetime
    by_owner: dict[str, list[int]] = {}
    for pr in prs:
        owner = pr_owner(pr.get("headRefName", ""), members)
        if not owner or pr.get("isDraft"):
            continue
        try:
            upd = datetime.datetime.fromisoformat(pr["updatedAt"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if now - upd > idle_s:
            by_owner.setdefault(owner, []).append(int(pr["number"]))
    out = []
    for owner, nums in sorted(by_owner.items()):
        nums.sort()
        out.append({"to": owner, "items": nums, "key": "idle-prs:" + ",".join(map(str, nums)),
                    "body": (f"{len(nums)} open PR(s) from your branches have had no activity "
                             f"for over {idle_s / 3600:.0f}h: " + ", ".join(f"#{n}" for n in nums)
                             + ". Finish, rebase, or close each; ack with what you did, or reply "
                               "why it should wait.")})
    return out


def summary(conn, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    by_member = {r[0]: r[1] for r in conn.execute(
        "SELECT recipient, COUNT(*) FROM msgs WHERE status = 'open' GROUP BY recipient "
        "ORDER BY recipient")}
    oldest = _rows(conn.execute(
        f"SELECT {', '.join(MSG_COLUMNS)} FROM msgs WHERE status = 'open' "
        "ORDER BY sent_at LIMIT 1"))
    o = oldest[0] if oldest else None
    closed_24h = conn.execute("SELECT COUNT(*) FROM msgs WHERE status != 'open' AND acked_at > ?",
                              (now - 86400,)).fetchone()[0]
    return {
        "open": sum(by_member.values()), "by_member": by_member, "closed_24h": closed_24h,
        "escalated": conn.execute("SELECT COUNT(*) FROM msgs WHERE status = 'open' AND "
                                  "escalated_to IS NOT NULL").fetchone()[0],
        "oldest": ({"id": o["id"], "to": o["recipient"], "from": o["sender"], "kind": o["kind"],
                    "age_s": int(now - o["sent_at"]), "escalated_to": o["escalated_to"]}
                   if o else None),
    }


# --- rendering (the block run_member.sh puts first in a pass's prompt) ---------------------

def render(msgs: list[dict], me: str, now: float | None = None) -> str:
    if not msgs:
        return ""
    now = time.time() if now is None else now
    lines = [
        f"## INBOX -- {len(msgs)} unread message(s) for {me}. Handle these BEFORE your charter.",
        "",
        "Other fleet members sent you these. For EACH one, before any other work: do what it "
        "asks, then record it --",
        f"  python3 /fleet-kit/scripts/fleet_msg.py ack --me {me} --id <id> --note \"<what you did, "
        "with issue/PR numbers>\"",
        "or, if you will not act, say why (a reply is a real answer; silence is not) --",
        f"  python3 /fleet-kit/scripts/fleet_msg.py reply --me {me} --id <id> --reason \"<why not>\"",
        "An unacked message escalates to jefe after 2 of your cadences, then to Reif. List what "
        "you acked under `Inbox:` in your report.",
    ]
    for m in msgs:
        items = ", ".join(f"#{i}" for i in m.get("items") or [])
        lines += ["", f"### message #{m['id']} -- {m['kind']} from {m['sender']}, "
                      f"{_age(now - m['sent_at'])} ago"
                      + (f" (escalated to {m['escalated_to']})" if m.get("escalated_to") else "")]
        if items:
            lines.append(f"items: {items}")
        lines.append(m["body"].strip())
    return "\n".join(lines)


def _connect(db_path: str | None):
    return fleet_db.connect(Path(db_path) if db_path else None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db-path", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--to", required=True, help="comma-separated members")
    s.add_argument("--kind", required=True)
    s.add_argument("--key", default=None, help="dedupe key (default: the kind)")
    s.add_argument("--body", required=True)
    s.add_argument("--items", default="", help="comma-separated issue/PR numbers")
    i = sub.add_parser("inbox")
    i.add_argument("--me", required=True)
    i.add_argument("--render", action="store_true")
    i.add_argument("--mark-read", action="store_true")
    for name in ("ack", "reply"):
        c = sub.add_parser(name)
        c.add_argument("--me", required=True)
        c.add_argument("--id", type=int, required=True)
        c.add_argument("--note" if name == "ack" else "--reason", dest="note", required=True)
    w = sub.add_parser("watchdog")
    w.add_argument("--dry-run", action="store_true")
    sub.add_parser("summary")
    ip = sub.add_parser("idle-prs", help="the-fixer: message each owner about PRs idle > 4h (gh, cwd repo)")
    ip.add_argument("--from", dest="sender", default="the-fixer")
    ip.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("pregate")
    g.add_argument("--me", required=True)
    a = ap.parse_args(argv)
    conn = _connect(a.db_path)

    if a.cmd == "send":
        items = [int(x) for x in re.findall(r"\d+", a.items)]
        sent = send(conn, a.sender, a.to, a.kind, a.key or a.kind, a.body, items)
        print(json.dumps(wake(conn, sent, a.kind)))
    elif a.cmd == "inbox":
        msgs = inbox(conn, a.me)
        if a.mark_read:
            mark_read(conn, [m["id"] for m in msgs])
        print(render(msgs, a.me) if a.render else json.dumps(msgs))
    elif a.cmd in ("ack", "reply"):
        try:
            print(json.dumps(close(conn, a.me, a.id, a.note, acted=a.cmd == "ack")))
        except ValueError as exc:
            print(f"fleet_msg: {exc}", file=sys.stderr)
            return 2
    elif a.cmd == "watchdog":
        print(json.dumps(watchdog(conn, dry_run=a.dry_run)))
    elif a.cmd == "summary":
        print(json.dumps(summary(conn)))
    elif a.cmd == "idle-prs":
        import subprocess
        r = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "100", "--json",
                            "number,headRefName,updatedAt,isDraft"],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            print(f"fleet_msg: gh pr list failed: {r.stderr[:200]}", file=sys.stderr)
            return 1
        msgs = idle_pr_messages(json.loads(r.stdout or "[]"), set(_specs()), time.time())
        sent = [] if a.dry_run else [
            send(conn, a.sender, [m["to"]], "pr-idle", m["key"], m["body"], m["items"])
            for m in msgs]
        print(json.dumps({"messages": msgs, "sent": sent}))
    elif a.cmd == "pregate":
        n = len(inbox(conn, a.me))
        print(f"FIRE {n} unread message(s)" if n else "green (inbox empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
