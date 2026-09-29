#!/usr/bin/env python3
"""ask.py -- a real channel for "a member hit a wall the fleet cannot act on" (gh#568).

THE GAP THIS CLOSES. Today the only channel is applying `fleet:needs-human-op` to an issue and
stopping. That label carries no structured `why`/`unblocks`/`proposed` -- a human reading it has
to reconstruct the ask from the issue body by hand -- and the fleet keeps no record of how it was
answered, so it cannot learn from its own past asks. This is the first buildable child of
fleet-kit#558's authority ladder: a `fleet.db` table (`asks`, see fleet_db.py's SCHEMA) plus this
CLI, so any member can call `ask.py file` instead of dead-ending on a label.

NON-GOALS (this issue's own body): no console or superadmin UI, no migration of existing
`fleet:needs-human-op` issues into asks. This is purely the record + CLI.

gh#771 (the authority ladder's first seam, `scripts/authority.py`) since added the one thing
this file's own NON-GOALS originally excluded: `ask.py file` now consults `authority.py` before
filing an open ask, so a class standing at `act`/`act-and-tell` proceeds instead of asking a
human the same question a third time. See `authority.py`'s own docstring for the ladder.

Pure core (`file_ask`/`answer_ask`/`list_asks`), thin DB seam (`fleet_db.connect`), CLI (`main`)
-- same split as cost_bridge.py / claim_history.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402
import authority  # noqa: E402  -- gh#771: class -> level, so filing twice doesn't ask a third time

ASK_COLUMNS = (
    "id", "member", "why", "unblocks", "proposed", "status",
    "answer", "answered_by", "answered_at", "filed_at", "class", "summary",
)

# gh#558 finding 4 ("the ladder has nothing to climb"): gh#650 narrowed this to just the two
# classes quality_gate.py's world-class seam needed (`decision`, `acceptance`), which silently
# dropped the rest of #558's own class list -- so every real ask filed by gru.md/
# dont-shoot-the-messenger.md landed with class=NULL and #771's authority ladder (class ->
# level) had nothing to promote. Restored to the full set #558 names: `credential`, `money`,
# `pricing`, `product-copy`, `infra`, `external-merge` alongside the two gh#650 already added,
# plus `idea` (the thinking-project ask class named in #558's later comments). A closed set,
# not an open string, so `--class banana` is still caught here rather than silently stored.
ASK_CLASSES = (
    "decision", "acceptance", "credential", "money", "pricing",
    "product-copy", "infra", "external-merge", "idea",
)


def file_ask(conn, member: str, why: str, unblocks: str | None = None,
            proposed: str | None = None, filed_at: float | None = None,
            ask_class: str | None = None, status: str = "open",
            answer: str | None = None, answered_by: str | None = None,
            answered_at: float | None = None, summary: str | None = None) -> int:
    """Insert a new ask. Returns its id.

    Defaults record a fresh open ask exactly as before this function grew the last five
    params -- gh#771's authority ladder reuses this same INSERT for a standing-grant record
    (status='act'/'notice', answer/answered_by/answered_at already filled in at filing time,
    see `main()`'s `file` command below) rather than duplicating the SQL, so `list_asks` and
    `answer_ask` never need to special-case how a row got its answered fields.

    `summary` (gh#877) is optional and defaults to NULL -- a caller that never passes it files
    exactly the row it always has (AC2); Home's renderer falls back to truncating `why` when
    this is NULL (AC5).
    """
    cur = conn.execute(
        "INSERT INTO asks (member, why, unblocks, proposed, status, answer, answered_by, "
        "answered_at, filed_at, class, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (member, why, unblocks, proposed, status, answer, answered_by, answered_at,
         filed_at if filed_at is not None else time.time(), ask_class, summary),
    )
    conn.commit()
    return cur.lastrowid


def answer_ask(conn, ask_id: int, answer: str, answered_by: str,
              status: str = "answered", answered_at: float | None = None) -> bool:
    """Answer an open ask exactly once. Returns True on success, False if `ask_id` does not
    exist or was already answered (AC3: a second call is a no-op, never a silent overwrite).

    The UPDATE's own `WHERE answered_at IS NULL` is what makes this atomic against a second,
    concurrent `answer` call racing this one -- whichever UPDATE's WHERE clause still matches
    wins, sqlite's single-writer lock serializes the two, and the loser's rowcount is 0.
    """
    cur = conn.execute(
        "UPDATE asks SET status = ?, answer = ?, answered_by = ?, answered_at = ? "
        "WHERE id = ? AND answered_at IS NULL",
        (status, answer, answered_by,
         answered_at if answered_at is not None else time.time(), ask_id),
    )
    conn.commit()
    return cur.rowcount == 1


def list_asks(conn, status: str | None = "open", member: str | None = None,
             limit: int = 100) -> list[dict]:
    q = f"SELECT {', '.join(ASK_COLUMNS)} FROM asks WHERE 1=1"
    params: list = []
    if status and status != "all":
        q += " AND status = ?"; params.append(status)
    if member:
        q += " AND member = ?"; params.append(member)
    q += " ORDER BY filed_at DESC LIMIT ?"
    params.append(limit)
    cur = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# Reif, 2026-09-28: "asks only from dumbledore." Any other member's ask is a message to him;
# he denies it with the other way (a fleet owner does it, access the fleet holds, a workaround,
# the opinion decided) -- "dumbledore actually denies the request because another way is
# found" -- and only a true one-way door escalates to Reif, by email under his name. One ask id
# end to end; the triage record is the closed `ask` message (its note starts denied:/escalated:).
TRIAGE = "dumbledore"


def route_to_triage(conn, ask_id: int, member: str, why: str, ask_class: str | None = None,
                    wake: bool = True) -> list[dict]:
    """Message dumbledore about ask #ask_id (kind `ask`, a wake kind: his pass starts now)."""
    import fleet_msg
    body = (f"ask #{ask_id} from {member} (class: {ask_class or 'unclassed'}): {why.strip()}\n\n"
            f"Deny it with the other way: `ask.py deny {ask_id} --me dumbledore --path \"...\" "
            f"[--to <member>]`. Only a true one-way door: `ask.py escalate {ask_id} --me "
            f"dumbledore --reason \"why no other way exists\"`.")
    sent = fleet_msg.send(conn, member, [TRIAGE], "ask", f"ask:{ask_id}", body)
    conn.executemany("UPDATE msgs SET ask_id = ? WHERE id = ?", [(ask_id, r["id"]) for r in sent])
    conn.commit()
    return fleet_msg.wake(conn, sent, "ask") if wake else sent


def _close_triage(conn, ask_id: int, member: str, note: str) -> None:
    import fleet_msg
    row = conn.execute("SELECT id FROM msgs WHERE recipient = ? AND kind = 'ask' AND ask_id = ? "
                       "ORDER BY id DESC LIMIT 1", (TRIAGE, ask_id)).fetchone()
    mid = row[0] if row else route_to_triage(conn, ask_id, member, note, wake=False)[0]["id"]
    fleet_msg.close(conn, TRIAGE, mid, note)


def triage_rate(conn, days: float = 7.0) -> dict:
    since = time.time() - days * 86400
    n, esc = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(ack_note LIKE 'escalated:%'), 0) FROM msgs "
        "WHERE recipient = ? AND kind = 'ask' AND status != 'open' AND acked_at > ?",
        (TRIAGE, since)).fetchone()
    return {"triaged": n, "escalated": esc, "denied": n - esc, "days": days,
            "rate": round(esc / n, 3) if n else 0.0}


def _notify(member: str, ask_id: int, why: str, sender: str | None = None) -> None:
    """One NTFY (+ email) per member per rolling hour, via the fleet's shared fleet_alert.sh
    channel -- same helper every other check pages through, so a filed ask reaches a human the
    same way an alarm does and gets the same undelivered-retry queue for free (AC4).

    Dedupe: `problem` is keyed to the ask id, so alert_store.py's own (check, problem) dedupe
    pages once per ask (a retry of the same ask is `SKIP already paged`). `severity=critical` is what makes that first page fire immediately rather
    than waiting on a debounce window: a fleet member blocked right now needs a human to see it
    now, not after a condition has "persisted."

    Best-effort only, same contract as fleet_alert.sh itself: a member's ask is already
    committed to fleet.db by the time this runs, so a failed/undeliverable page must never make
    `ask.py file` itself fail.
    """
    import os
    script = HERE / "fleet_alert.sh"
    # fk#1383: only dumbledore's one-way-door escalations page now, so each ask gets its own
    # page; a per-member hour bucket silently dropped a second escalation for the same member.
    problem = f"{member}:ask{ask_id}"
    # #1049 made fleet_alert.sh's email leg opt-in and Reif has no ntfy app, so without this an
    # ask reaches nobody. Reif, 2026-09-15: the console inbox is gone; "somehow it can get me a
    # message some other way if it needs me." An ask is that message: force the email leg.
    # Reif, 2026-09-28: "label these as officially from dumbledore" -- this is the one place
    # in the fleet that pages Reif for an ask, so it brands both ends: the From name (MAIL_FROM,
    # read by fleet_alert.sh's _send_email) and a "[dumbledore]" subject prefix. inbox.py's reply
    # matching keys off the message BODY ("yes N"/"no N: ...") and only checks the subject for a
    # leading Re:/Fwd:, never the literal "fleet ask #N" text, so the prefix cannot break a reply.
    env = dict(os.environ, FLEET_ALERT_EMAIL_LEG="1",
              MAIL_FROM="Dumbledore (fleet) <hello@philanthropy.org>")
    sender = sender or member
    title = f"[dumbledore] fleet ask #{ask_id} from {sender}" + (f" (for {member})" if sender != member else "")
    # fk#1056: the mail says how to answer it, and a reply to it reaches the fleet (Reply-To
    # is set by fleet_alert.sh; webhook_receiver.py applies the answer with no model in the way).
    body = (f"ask #{ask_id}: {why}\n\n"
            f"Reply to this email with one line: `yes {ask_id}`, `no {ask_id}: why`, "
            f"or `{ask_id}: your answer`. Anything else you write goes to the messenger.")
    try:
        subprocess.run(
            ["bash", str(script), "--check", "ask", "--problem", problem,
             "--severity", "critical", "--handle", member, title, body],
            capture_output=True, timeout=30, check=False, env=env,
        )
    except Exception:
        pass


LANE_FOR_CLASS = {"credential": "lane:devops", "infra": "lane:devops", "money": "lane:revenue",
                  "pricing": "lane:revenue", "product-copy": "lane:ui"}


def _repo_slug() -> str:
    import os
    url = (os.environ.get("FLEET_REPO_URL") or "").strip()
    slug = url.rsplit("github.com", 1)[-1].lstrip(":/").removesuffix(".git").strip("/")
    return slug if slug.count("/") == 1 and all(slug.split("/")) else ""


def issue_for_ask(ask_id: int, member: str, why: str, unblocks: str | None, proposed: str | None,
                  ask_class: str | None, run=None) -> str:
    """Reif, 2026-09-16, on a gru report that had been blind for 13h on an open ask: "if
    something is broken like this - I want to make darn sure that another agent picks it up."
    An ask used to live only in fleet.db, where no builder looks. Now every open ask also
    files ONE fleet:backlog issue in the product repo, priority-high, laned by class, titled
    `ask #N`, so gru/marie rank it and a builder tries it; the ask row still waits for the
    human. Opt-in (FLEET_ASK_ISSUES=1). Idempotent by title. Returns the URL or ''."""
    import os
    if os.environ.get("FLEET_ASK_ISSUES") != "1":
        return ""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    slug = _repo_slug()
    if not slug:
        return ""
    title = f"ask #{ask_id} ({ask_class or 'unclassed'}): {why.strip().splitlines()[0][:90]}"
    have = run(["gh", "issue", "list", "--repo", slug, "--state", "open", "--search",
                f'"ask #{ask_id} " in:title', "--json", "url", "--jq", ".[0].url"])
    if have.returncode == 0 and have.stdout.strip():
        return have.stdout.strip()
    labels = ["fleet:backlog", "fleet:priority-high", LANE_FOR_CLASS.get(ask_class or "", "lane:coordination")]
    body = (f"Filed by `{member}` as fleet ask #{ask_id} (class: {ask_class or 'unclassed'}).\n\n"
            f"**Why a human was asked:** {why.strip()}\n\n"
            + (f"**What it unblocks:** {unblocks.strip()}\n\n" if unblocks else "")
            + (f"**Proposed:** {proposed.strip()}\n\n" if proposed else "")
            + "An agent does this now, with the fleet's own tokens, env and tools. Only rotating or "
              "exposing a secret, spending money, deleting prod data, force-pushing main or a login "
              "only Reif holds waits on him -- ship everything around it and close the rest.\n\n"
            # jefe msg#162 (#8428): the intake gates read these two lines; without them every
            # ask issue sat in fleet:needs-spec until marie backfilled it by hand.
            + "## Acceptance\n"
              "- The blocker named above is gone: the fix ships and this issue closes.\n\n"
            + "Vision-link: none (maintenance) -- a provisioning ask, not a direct KR mover")
    r = run(["gh", "issue", "create", "--repo", slug, "--title", title, "--label", ",".join(labels), "--body", body])
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""


def settle_ask_issue(ask_id: int, outcome: str, note: str, run=None) -> str:
    """jefe msgs #342/#351: the issue issue_for_ask mirrored used to outlive its ask. A denied or
    answered ask left an open priority-high backlog item that gru's gates bounced to
    fleet:needs-spec and marie backfilled by hand (philanthropy 8648-8652 and 8677: all denied, all still
    cycling). Now the answer settles the mirror: escalated -> fleet:needs-human-op (the
    NOT_FOR_MINIONS label gate_drops.py skips), anything else -> closed with the answer as the
    closing comment. Returns the issue URL it touched, or ''."""
    import os
    if os.environ.get("FLEET_ASK_ISSUES") != "1":
        return ""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    slug = _repo_slug()
    if not slug:
        return ""
    found = run(["gh", "issue", "list", "--repo", slug, "--state", "open", "--search",
                 f'"ask #{ask_id} " in:title', "--json", "number,title,url"])
    if found.returncode != 0:
        return ""
    hits = [i for i in json.loads(found.stdout or "[]") if i["title"].startswith(f"ask #{ask_id} ")]
    if not hits:
        return ""
    num = str(hits[0]["number"])
    if outcome == "escalated":
        run(["gh", "issue", "edit", num, "--repo", slug, "--add-label", "fleet:needs-human-op",
             "--remove-label", "fleet:needs-spec"])
        run(["gh", "issue", "comment", num, "--repo", slug, "--body",
             f"Ask #{ask_id} escalated to Reif (a true one-way door): {note}"])
    else:
        run(["gh", "issue", "close", num, "--repo", slug, "--reason", "not planned", "--comment",
             f"Ask #{ask_id} {outcome}: {note}\n\nThe work, if any, went to the member named "
             "there; this mirror has nothing left to build."])
    return hits[0]["url"]


def _settle(ask_id: int, outcome: str, note: str) -> None:
    try:
        url = settle_ask_issue(ask_id, outcome, note)
        if url:
            print(f"ask {ask_id} board issue settled ({outcome}): {url}")
    except Exception:  # noqa: BLE001 -- best-effort, the ask row is already answered
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="File, answer, or list fleet asks -- way rare: only a one-way door the fleet "
                     "must not walk alone. Opinion classes file as notices (gh#568, persona_law 2b). "
                     "Only dumbledore reaches Reif: any other member's ask goes to dumbledore, who "
                     "denies it with the other way or, for a true one-way door, escalates it.")
    ap.add_argument("--db-path", help="override fleet.db path (default: fleet_db.DB_FILE)")
    ap.add_argument("--authority-path",
                    help="override authority.json path (default: authority.STORE)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_file = sub.add_parser("file", help="file a new ask")
    p_file.add_argument("--member", required=True)
    p_file.add_argument("--why", required=True,
                        help="why this needs Reif -- way rare: only a one-way door (secret, spend, "
                             "prod data delete, force-push main, his own login); opinions are "
                             "yours to decide (persona_law.md 2b). It goes to dumbledore, not "
                             "Reif: he denies it with the other way, or escalates a true one-way door")
    p_file.add_argument("--unblocks", help="what gets unstuck once this is answered")
    p_file.add_argument("--proposed", help="a proposed answer, if the filer has one")
    p_file.add_argument("--summary",
                        help="one plain-language sentence for a reader who has never seen the "
                             "codebase -- no identifiers, paths, or exit codes; Home renders "
                             "this as the card's headline instead of --why (optional)")
    p_file.add_argument("--class", dest="ask_class", choices=ASK_CLASSES,
                        help=f"ask class: {', '.join(ASK_CLASSES)} (optional)")
    p_file.add_argument("--no-notify", action="store_true",
                        help="skip the page/triage message (tests, or a caller paging separately)")

    p_deny = sub.add_parser("deny", help="dumbledore: deny an open ask with the other way found")
    p_deny.add_argument("id", type=int)
    p_deny.add_argument("--me", required=True)
    p_deny.add_argument("--path", required=True, help="the other way, written down")
    p_deny.add_argument("--to", help="the member who does it (gets the work, woken now)")

    p_esc = sub.add_parser("escalate", help="dumbledore: email Reif a true one-way door")
    p_esc.add_argument("id", type=int)
    p_esc.add_argument("--me", required=True)
    p_esc.add_argument("--reason", required=True, help="why no other way exists")

    p_rate = sub.add_parser("triage-rate", help="dumbledore's escalated / triaged")
    p_rate.add_argument("--days", type=float, default=7.0)

    p_answer = sub.add_parser("answer", help="answer an open ask exactly once")
    p_answer.add_argument("id", type=int)
    p_answer.add_argument("--answer", required=True)
    p_answer.add_argument("--answered-by", required=True)
    p_answer.add_argument("--status", default="answered")

    p_list = sub.add_parser("list", help="list asks")
    p_list.add_argument("--status", default="open", help="open|answered|all (default: open)")
    p_list.add_argument("--member")
    p_list.add_argument("--limit", type=int, default=100)

    p_authority = sub.add_parser(
        "authority", help="report the standing grants a class -> level authority.json holds (gh#771)")
    p_authority.add_argument("--class", dest="ask_class",
                             help="show only this class's grant (default: every grant)")

    a = ap.parse_args(argv)
    conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
    authority_store = Path(a.authority_path) if a.authority_path else None

    if a.cmd == "file":
        try:
            level = authority.level_for(a.ask_class, store=authority_store)
        except authority.AuthorityError as exc:
            print(f"ask.py: {exc}", file=sys.stderr)
            return 1

        if level == "ask":
            # AC1: no grant (or no --class at all) -- byte-identical to every prior release
            # (plus AC2's summary=None when --summary is omitted).
            ask_id = file_ask(conn, a.member, a.why, a.unblocks, a.proposed, ask_class=a.ask_class,
                              summary=a.summary)
            print(f"ask {ask_id} filed")
            if not a.no_notify:
                if a.member == TRIAGE:
                    _notify(a.member, ask_id, a.why)
                else:
                    route_to_triage(conn, ask_id, a.member, a.why, a.ask_class)
                    print(f"ask {ask_id} sent to {TRIAGE} to triage")
                try:
                    url = issue_for_ask(ask_id, a.member, a.why, a.unblocks, a.proposed, a.ask_class)
                    if url:
                        print(f"ask {ask_id} also on the board: {url}")
                except Exception:  # noqa: BLE001 -- best-effort, the ask is already filed
                    pass
            return 0

        row = authority.show(store=authority_store).get(a.ask_class, {})
        granted_by = row.get("granted_by", "default (Reif 2026-09-28: decide it yourself)")
        now = time.time()
        if level == "act":
            # AC2/AC3: proceed, exit 0, no open row -- but a countable record still lands,
            # carrying class (column), member (column) and the granting authority (answered_by).
            answer = f"authorized under standing grant by {granted_by} (class={a.ask_class})"
            ask_id = file_ask(conn, a.member, a.why, a.unblocks, a.proposed, ask_class=a.ask_class,
                              status="act", answer=answer, answered_by=f"authority:{granted_by}",
                              answered_at=now, filed_at=now, summary=a.summary)
            print(f"ask {ask_id} authorized -- standing grant by {granted_by} "
                  f"(class={a.ask_class}); proceed")
            return 0

        # level == "act-and-tell" (the only other value authority.level_for can return):
        # AC4: proceed, file a NOTICE -- a status distinct from 'open', never blocking.
        answer = f"notice filed under standing act-and-tell grant by {granted_by} (class={a.ask_class})"
        ask_id = file_ask(conn, a.member, a.why, a.unblocks, a.proposed, ask_class=a.ask_class,
                          status="notice", answer=answer, answered_by=f"authority:{granted_by}",
                          answered_at=now, filed_at=now, summary=a.summary)
        print(f"ask {ask_id} filed as notice -- act-and-tell grant by {granted_by} "
              f"(class={a.ask_class}); proceed")
        return 0

    if a.cmd == "answer":
        ok = answer_ask(conn, a.id, a.answer, a.answered_by, a.status)
        if not ok:
            exists = conn.execute("SELECT 1 FROM asks WHERE id = ?", (a.id,)).fetchone()
            reason = "already answered" if exists else "no such ask"
            print(f"ask.py: answer {a.id}: {reason}", file=sys.stderr)
            return 1
        _settle(a.id, a.status, a.answer)
        print(f"ask {a.id} answered")
        return 0

    if a.cmd in ("deny", "escalate"):
        if a.me != TRIAGE:
            print(f"ask.py: only {TRIAGE} triages asks", file=sys.stderr)
            return 2
        row = conn.execute("SELECT member, why, answered_at FROM asks WHERE id = ?", (a.id,)).fetchone()
        if not row or row[2] is not None:
            print(f"ask.py: {a.cmd} {a.id}: {'already answered' if row else 'no such ask'}", file=sys.stderr)
            return 1
        member, why = row[0], row[1]
        if a.cmd == "escalate":
            _notify(member, a.id, f"{why}\n\n{TRIAGE}: no other way -- {a.reason}", sender=TRIAGE)
            _close_triage(conn, a.id, member, f"escalated: {a.reason}")
            _settle(a.id, "escalated", a.reason)
            print(f"ask {a.id} escalated to Reif")
            return 0
        answer_ask(conn, a.id, f"denied, another way: {a.path}", TRIAGE, status="denied")
        _close_triage(conn, a.id, member, f"denied: {a.path}")
        _settle(a.id, "denied", a.path)
        if a.to:
            import fleet_msg
            sent = fleet_msg.send(conn, TRIAGE, [a.to], "nudge", f"ask:{a.id}",
                                  f"ask #{a.id} from {member} was denied because another way "
                                  f"exists, and it is yours: {a.path}\n\nOriginal: {why}")
            fleet_msg.wake(conn, sent, "nudge")
        print(f"ask {a.id} denied" + (f", sent to {a.to}" if a.to else ""))
        return 0

    if a.cmd == "triage-rate":
        r = triage_rate(conn, a.days)
        print(f"Escalation-rate: {r['escalated']}/{r['triaged']} ({r['rate']:.0%}) over {a.days:g}d")
        return 0

    if a.cmd == "list":
        print(json.dumps(list_asks(conn, status=a.status, member=a.member, limit=a.limit),
                         indent=2))
        return 0

    if a.cmd == "authority":
        try:
            grants = authority.show(store=authority_store)
        except authority.AuthorityError as exc:
            print(f"ask.py: {exc}", file=sys.stderr)
            return 1
        if a.ask_class:
            grants = {a.ask_class: grants[a.ask_class]} if a.ask_class in grants else {}
        print(json.dumps(grants, indent=2, sort_keys=True))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
