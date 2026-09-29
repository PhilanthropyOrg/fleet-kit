#!/usr/bin/env python3
"""gate_drops -- gru's two eligibility gates, with every drop made loud (philanthropy#8215).

THE GAP THIS CLOSES. Reif, 2026-09-26: "so gru passes on something with no quality label and
then never alerts anyone?" vision_link_gate.py and quality_gate.py name their drops, but the
names only ever reached gru.log: 26 of 30 fleet:priority-high items were dropped that day and
nobody was told. This runs both gates in their fixed order (Vision-link, then quality) and,
for each drop:

  * mechanical gap -> fixed here. Any non-epic item with no `quality:` label gets
    `quality:solid` (philanthropy#8218 step 1: solid is the default bar; this was Reif's items
    only) before the gates run, so it can still be built THIS pass.
  * spec gap (no Vision-link, no quality label on an ordinary item, no Given/When/Then, two
    quality labels) -> `fleet:needs-spec` + ONE comment naming the missing piece (idempotent:
    the same gap is never re-commented) + a single ask per pass for the next brief, listing
    only the items newly labeled this pass.
  * by-design drop (a `fleet:epic` tracking parent, a world-class item waiting on its VP
    design review) -> recorded, nothing else: the process already owns it.

Every drop is appended to $FLEET_LOG_DIR/gate_drops.jsonl; the console's "Gate drops" tile
(`count`) reads it. An item that carries `fleet:needs-spec` and now clears both gates has the
label removed, so it re-enters normally.

The two gates stay the pure cores; `plan()` is pure too (unit-tested, no gh), `apply()` is the
thin exec seam -- same split as dead_end_label.py.

INTAKE (philanthropy#8218 step 3). `run` only sees gru's current tier, so 448 backlog items sat
unlabeled and unspecced. `intake` runs the same plan()/apply() over EVERY open fleet:backlog
item before each gru pass (run_gru_fanout.sh), sends marie + jefe ONE message naming the items
it newly labeled or found at a new gap (re-sending any still stuck FLEET_GATE_DROP_RESEND_H
after its last message), and sends `hq` ONE message listing open `fleet:needs-prod-access` items
(prod DB, secrets, Cloudflare: HQ holds that access, minions don't).

Usage:
  gate_drops.py run --items /tmp/gru_items.json [--run-id ID] [--repo O/R] [--dry-run]
  gate_drops.py intake [--repo O/R] [--dry-run]
  gate_drops.py count [--hours 6]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quality_gate  # noqa: E402
import vision_link_gate  # noqa: E402
from board_github import LABEL_CLAIMED, LABEL_PROD_ACCESS, NOT_FOR_MINIONS  # noqa: E402
from items_arg import HELP as ITEMS_HELP, load_items  # noqa: E402

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
NEEDS_SPEC = f"{PREFIX}needs-spec"
DEFAULT_QUALITY = "quality:solid"
MARKER = "needs-spec:"
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
DROP_LOG = LOG_DIR / "gate_drops.jsonl"
# philanthropy#8215 amendment: more than this many needs-spec drops in one pass messages
# marie (who fixes specs) and jefe (who watches why they keep arriving unspecced).
MSG_THRESHOLD = int(os.environ.get("FLEET_GATE_DROP_MSG_THRESHOLD", "3"))
MSG_TO = ("marie", "jefe")
HQ = "hq"
# intake leaves these alone: in flight, not minion work at all, or a tracking-only epic.
INTAKE_SKIP = {LABEL_CLAIMED, *NOT_FOR_MINIONS, quality_gate.EPIC}
# An item still stuck at a gate this long after marie was last told about it is sent again.
# 2026-09-29: 32 items sat re-dropped every 30 min for 2.5 days with no message. marie fixed one
# gap (the vision-link) and the item stayed labeled, so its next gap (acceptance) was never sent.
RESEND_S = float(os.environ.get("FLEET_GATE_DROP_RESEND_H", "72")) * 3600

# What each gate's drop reason means for the person who has to fix it.
GAPS = (
    ("no Vision-link", "vision-link",
     "a `Vision-link:` line naming a KR id from scripts/okr.json, or `Vision-link: none (maintenance)`"),
    ("no quality: label", "quality-label",
     "exactly one `quality:ship-it` / `quality:solid` / `quality:world-class` label"),
    ("more than one quality label", "quality-label",
     "exactly one `quality:` label (it has several)"),
    ("no Given/When/Then", "acceptance",
     "at least one Given/When/Then acceptance criterion in the body or the newest PRD comment"),
)
BY_DESIGN = ("tracking-only parent", "world-class with no")


def _names(labels) -> list[str]:
    return [lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in labels or []]


def gap_for(reason: str) -> tuple[str, str] | None:
    """(key, what's missing) for a spec gap, None for a by-design drop."""
    if reason.startswith(BY_DESIGN):
        return None
    for prefix, key, missing in GAPS:
        if reason.startswith(prefix):
            return key, missing
    return "other", reason


def comment_body(key: str, missing: str, reason: str, run_id: str) -> str:
    return (f"{MARKER} {key}\n\n"
            f"gru skipped this item (pass {run_id}) because it is missing {missing}.\n\n"
            f"Gate said: {reason}\n\n"
            f"Add the missing piece and the next gru pass picks it up; the `{NEEDS_SPEC}` label "
            f"comes off by itself once it clears the gate.")


def _has_marker(comments, key: str) -> bool:
    line = f"{MARKER} {key}"
    return any((c.get("body") or "").lstrip().startswith(line) for c in comments or [])


def parent_refs(items: list[dict]) -> set[int]:
    """Issue numbers named by a `Vision-link: #N` line (vision_link_gate.parent_ref)."""
    refs = set()
    for it in items:
        status, raw = vision_link_gate.classify_candidate(it.get("body"), it.get("comments"))
        n = vision_link_gate.parent_ref(raw) if status == vision_link_gate.STATUS_MISSING else None
        if n is not None:
            refs.add(n)
    return refs


def fetch_parents(items: list[dict], known: list[dict], repo: str | None, run=None) -> dict:
    """{N: {body, comments}} for every `Vision-link: #N` parent: from `known` when it is
    already in hand, else one `gh issue view` each. A parent that won't load is left out."""
    have = {it["number"]: it for it in known}
    out = {}
    for n in parent_refs(items):
        if n in have:
            out[n] = have[n]
            continue
        r = (run or _gh)(["gh", "issue", "view", str(n), "--json", "number,body,comments",
                          *(["--repo", repo] if repo else [])])
        if r.returncode == 0:
            try:
                out[n] = json.loads(r.stdout or "{}")
            except ValueError:
                pass
    return out


# `gh issue list --json comments` returns only an issue's OLDEST 100 comments (it does not
# paginate; `gh issue view` does). philanthropy#6850 (108 comments) was dropped as needs-spec
# while its Given/When/Then sat in comment #102 -- and every later fix lands past the cap too.
LIST_COMMENT_CAP = 100


def fill_capped_comments(items: list[dict], repo: str | None, run=None) -> list[dict]:
    """Refetch the full comment list, in place, for any item that hit the list cap."""
    for it in items:
        if len(it.get("comments") or []) < LIST_COMMENT_CAP:
            continue
        try:
            r = (run or _gh)(["gh", "issue", "view", str(it["number"]), "--json", "comments",
                              *(["--repo", repo] if repo else [])], 120)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            try:
                it["comments"] = json.loads(r.stdout or "{}").get("comments") or it["comments"]
            except ValueError:
                pass
    return items


def plan(items: list[dict], run_id: str, parents: dict | None = None) -> dict:
    """Pure. Decides every label/comment/ask and the final eligible list; runs no gh.
    `parents`: fetch_parents() output, so `Vision-link: #<epic>` inherits the epic's link."""
    actions: list[dict] = []   # {"number", "op": add_label|remove_label|comment, ...}
    fixed: set[int] = set()
    patched = []
    for it in items:
        labels = _names(it.get("labels"))
        # Mechanical: no quality label -> the default bar, before gating (epics never build).
        if quality_gate.EPIC not in labels and not set(labels) & set(quality_gate.QUALITY_LABELS):
            actions.append({"number": it["number"], "op": "add_label", "label": DEFAULT_QUALITY})
            fixed.add(it["number"])
            it = dict(it, labels=list(it.get("labels") or []) + [{"name": DEFAULT_QUALITY}])
        patched.append(it)
    items = patched
    by_num = {it["number"]: it for it in items}
    vis = vision_link_gate.gate_candidates(items, parents)
    survivors = [by_num[n] for n in vis["eligible"]]
    qual = quality_gate.gate_candidates(survivors)

    drops = [dict(d, gate="vision-link") for d in vis["dropped"]]
    drops += [dict(d, gate="quality") for d in qual["dropped"]]
    eligible = list(qual["eligible"])
    records: list[dict] = [{"number": n, "gate": "quality", "reason": "no quality: label",
                            "action": "fixed"} for n in eligible if n in fixed]
    newly: list[dict] = []

    for d in drops:
        it = by_num[d["number"]]
        labels = _names(it.get("labels"))
        rec = {"number": d["number"], "gate": d["gate"], "reason": d["reason"]}
        gap = gap_for(rec["reason"])
        if gap is None:
            records.append(dict(rec, action="by-design"))
            continue
        key, missing = gap
        if NEEDS_SPEC not in labels:
            actions.append({"number": d["number"], "op": "add_label", "label": NEEDS_SPEC})
            newly.append({"number": d["number"], "missing": missing})
        if not _has_marker(it.get("comments"), key):
            actions.append({"number": d["number"], "op": "comment",
                            "body": comment_body(key, missing, rec["reason"], run_id)})
        records.append(dict(rec, action="needs-spec", gap=key))

    # Keep marie's order: eligible in the order the items came in.
    order = {it["number"]: i for i, it in enumerate(items)}
    eligible = sorted(set(eligible), key=order.get)
    for n in eligible:
        if NEEDS_SPEC in _names(by_num[n].get("labels")):
            actions.append({"number": n, "op": "remove_label", "label": NEEDS_SPEC})

    ask = None
    if newly:
        listed = "; ".join(f"#{x['number']} needs {x['missing']}" for x in newly)
        ask = {
            "why": (f"gru dropped {len(newly)} item(s) at its build gates this pass and labeled "
                    f"them {NEEDS_SPEC}, each with a comment naming the gap: {listed}"),
            "summary": (f"{len(newly)} backlog item(s) can't be built until someone adds the "
                        f"missing spec"),
            "unblocks": "those items become buildable on the next gru pass",
            "proposed": "marie adds the missing piece in her next pass; reply only to override",
        }
    return {"eligible": eligible, "dropped": records, "actions": actions, "ask": ask,
            "message": message(records, len(items), run_id)}


def message(records: list[dict], candidates: int, run_id: str) -> dict | None:
    """The fleet_msg.py message gru sends marie + jefe when more than MSG_THRESHOLD items
    still need a spec after this pass (Reif's spec amendment on #8215: "shouldn't both jefe
    and marie get a notice?"). Pure; None at or under the threshold."""
    needs = [r for r in records if r.get("action") == "needs-spec"]
    if len(needs) <= MSG_THRESHOLD:
        return None
    by_gap: dict[str, list[int]] = {}
    for r in needs:
        by_gap.setdefault(r.get("gap") or "other", []).append(r["number"])
    lines = [f"gru's build gates dropped {len(needs)} of {candidates} candidates in pass "
             f"{run_id} for a missing spec piece; each carries `{NEEDS_SPEC}` and a "
             f"`{MARKER} <gap>` comment. Fix them so gru can build them:"]
    for gap, nums in sorted(by_gap.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {gap} ({len(nums)}): " + ", ".join(f"#{n}" for n in nums))
    return {"to": MSG_TO, "kind": "gate-drop", "key": "gate-drops",
            "body": "\n".join(lines), "items": [r["number"] for r in needs]}


def _gh(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --- intake: the whole backlog, every pass (philanthropy#8218 step 3) -----------------------

def intake_message(records: list[dict], newly: list[int], run_id: str,
                   stale: list[int] = ()) -> dict | None:
    """ONE message to marie + jefe naming the items this intake run newly labeled needs-spec
    or found at a new gap, plus `stale` ones still stuck RESEND_S after they were last sent.
    Pure. Keyed on the item set, so a new batch is never swallowed by the bus's 6h dedupe."""
    items = sorted(set(newly) | set(stale))
    if not items:
        return None
    gap_of = {r["number"]: r.get("gap") or "other" for r in records if r.get("action") == "needs-spec"}
    by_gap: dict[str, list[int]] = {}
    for n in items:
        by_gap.setdefault(gap_of.get(n, "other"), []).append(n)
    lines = [f"backlog intake ({run_id}): {len(items)} item(s) carry `{NEEDS_SPEC}` and a "
             f"`{MARKER} <gap>` comment. Add the missing piece so gru can build them:"]
    for gap, nums in sorted(by_gap.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {gap} ({len(nums)}): " + ", ".join(f"#{n}" for n in nums))
    if stale:
        lines.append(f"Sent again, still stuck {RESEND_S / 3600:.0f}h after the last message: "
                     + ", ".join(f"#{n}" for n in sorted(stale)) + ". If one can never be "
                     "specced from its own text, close it (Part B) instead of leaving it here.")
    return {"to": MSG_TO, "kind": "gate-drop", "key": "intake:" + ",".join(map(str, items)),
            "body": "\n".join(lines), "items": items}


def last_sent_by_item(conn) -> dict[int, float]:
    """Newest gate-drop message time per item number, from the fleet_msg bus."""
    out: dict[int, float] = {}
    for items, sent_at in conn.execute("SELECT items, sent_at FROM msgs WHERE kind = 'gate-drop'"):
        try:
            nums = json.loads(items or "[]")
        except ValueError:
            continue
        for n in nums if isinstance(nums, list) else []:
            if isinstance(n, int) and sent_at > out.get(n, 0):
                out[n] = sent_at
    return out


def prod_access_message(issues: list[dict]) -> dict | None:
    """ONE message to hq listing open fleet:needs-prod-access items. Pure; None when none."""
    nums = sorted(i["number"] for i in issues if LABEL_PROD_ACCESS in _names(i.get("labels")))
    if not nums:
        return None
    titles = {i["number"]: i.get("title") or "" for i in issues}
    body = "\n".join([f"{len(nums)} open item(s) need prod access (DB, secrets, Cloudflare) no "
                      f"minion has. Take them, or reply with why not:"] +
                     [f"- #{n} {titles[n]}" for n in nums])
    return {"to": (HQ,), "kind": "prod-access", "key": "prod-access:" + ",".join(map(str, nums)),
            "body": body, "items": nums}


def intake_plan(backlog: list[dict], prod_access: list[dict], run_id: str,
                parents: dict | None = None, last_sent: dict[int, float] | None = None,
                now: float | None = None) -> dict:
    """Pure. plan() over every open backlog item except claimed / human-op / prod-access / epics;
    the Reif ask is replaced by one bus message (an hourly sweep of 400+ items would otherwise
    ask Reif every hour), plus hq's prod-access message."""
    todo = [i for i in backlog if not set(_names(i.get("labels"))) & INTAKE_SKIP]
    p = plan(todo, run_id, parents)
    # A new gap comment counts too: an item already labeled that moved on to its next gap.
    newly = sorted({a["number"] for a in p["actions"] if a["op"] == "comment" or
                    (a["op"] == "add_label" and a["label"] == NEEDS_SPEC)})
    stale = []
    if last_sent is not None:
        now = now or time.time()
        stale = sorted(r["number"] for r in p["dropped"] if r.get("action") == "needs-spec"
                       and r["number"] not in newly and now - last_sent.get(r["number"], 0) >= RESEND_S)
    p["ask"] = None
    p["message"] = intake_message(p["dropped"], newly, run_id, stale)
    p["messages"] = [m for m in (prod_access_message(prod_access),) if m]
    p["scanned"], p["skipped"] = len(backlog), len(backlog) - len(todo)
    return p


INTAKE_FIELDS = "number,title,labels,body,comments,createdAt"


def list_open(label: str, repo: str | None, fields: str = INTAKE_FIELDS, run=None) -> list[dict]:
    r = (run or _gh)(["gh", "issue", "list", "--state", "open", "--label", label, "--limit", "2000",
             "--json", fields, *(["--repo", repo] if repo else [])])
    if r.returncode != 0:
        raise RuntimeError(f"gh issue list --label {label} failed: {(r.stderr or '')[:200]}")
    return json.loads(r.stdout or "[]")


def apply(p: dict, repo: str | None, run_id: str, run=_gh, db_path: str | None = None) -> dict:
    """Exec seam: gh calls, the drop log, and the ask. Best-effort per action, reported."""
    repo_args = ["--repo", repo] if repo else []
    results = []
    for a in p["actions"]:
        if a["op"] == "comment":
            cmd = ["gh", "issue", "comment", str(a["number"]), *repo_args, "--body", a["body"]]
        else:
            flag = "--add-label" if a["op"] == "add_label" else "--remove-label"
            cmd = ["gh", "issue", "edit", str(a["number"]), *repo_args, flag, a["label"]]
        r = run(cmd)
        if r.returncode != 0 and a["op"] == "add_label" and "not found" in (r.stderr or ""):
            run(["gh", "label", "create", a["label"], *repo_args, "--color", "D93F0B",
                 "--description", "gru dropped it at a build gate; the comment names the gap"])
            r = run(cmd)
        results.append(dict(a, ok=r.returncode == 0, **({} if r.returncode == 0 else
                                                        {"error": (r.stderr or "")[:200]})))
    now = time.time()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with DROP_LOG.open("a") as fh:
            for rec in p["dropped"]:
                fh.write(json.dumps(dict(rec, ts=now, run_id=run_id)) + "\n")
    except OSError as exc:
        print(f"gate_drops: could not append {DROP_LOG}: {exc}", file=sys.stderr)
    ask_id = None
    if p["ask"]:
        import ask as ask_mod
        import fleet_db
        conn = fleet_db.connect(Path(db_path) if db_path else None)
        # No page: the brief carries it (messenger_brief.asks_open), one voice to Reif.
        ask_id = ask_mod.file_ask(conn, "gru", p["ask"]["why"], p["ask"]["unblocks"],
                                  p["ask"]["proposed"], ask_class="decision",
                                  summary=p["ask"]["summary"])
    sent = None
    for m in [x for x in (p.get("message"), *p.get("messages", [])) if x]:
        try:
            import fleet_db
            import fleet_msg
            conn = fleet_db.connect(Path(db_path) if db_path else None)
            sent = (sent or []) + fleet_msg.send(conn, "gru", list(m["to"]), m["kind"], m["key"],
                                                 m["body"], m["items"])
        except Exception as exc:  # noqa: BLE001 -- the gates' result must still reach gru
            print(f"gate_drops: message not sent: {exc}", file=sys.stderr)
    return {"results": results, "ask_id": ask_id, "sent": sent}


def count_actions(records: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in records:
        out[r.get("action") or "?"] = out.get(r.get("action") or "?", 0) + 1
    return out


def count(hours: float = 6.0, now: float | None = None, path: Path | None = None) -> dict:
    """Distinct items dropped in the window (an item dropped every hour counts once)."""
    now = now or time.time()
    by_action: dict[str, set] = {}
    try:
        lines = (path or DROP_LOG).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if now - float(rec.get("ts") or 0) <= hours * 3600:
            by_action.setdefault(rec.get("action") or "?", set()).add(rec.get("number"))
    items = set().union(*by_action.values()) if by_action else set()
    return {"items": len(items), **{k: len(v) for k, v in sorted(by_action.items())}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="gate the candidates and make every drop visible")
    r.add_argument("--items", required=True, help="{number, labels, body, comments} list: " + ITEMS_HELP)
    r.add_argument("--run-id", default=os.environ.get("FLEET_RUN_ID") or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    r.add_argument("--repo", default=None)
    r.add_argument("--db-path", default=None)
    r.add_argument("--dry-run", action="store_true", help="print the plan, run no gh, write nothing")
    i = sub.add_parser("intake", help="gate EVERY open fleet:backlog item (philanthropy#8218)")
    i.add_argument("--run-id", default="intake-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    i.add_argument("--repo", default=None)
    i.add_argument("--db-path", default=None)
    i.add_argument("--dry-run", action="store_true", help="print the plan, run no gh writes")
    c = sub.add_parser("count", help="distinct items dropped in the last N hours")
    c.add_argument("--hours", type=float, default=6.0)
    a = ap.parse_args(argv)
    if a.cmd == "count":
        print(json.dumps(count(a.hours)))
        return 0
    if a.cmd == "intake":
        try:
            backlog = list_open(f"{PREFIX}backlog", a.repo, run=lambda c: _gh(c, 300))
            prod = list_open(LABEL_PROD_ACCESS, a.repo, "number,title,labels")
            fill_capped_comments(backlog, a.repo)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"gate_drops intake: {exc}", file=sys.stderr)
            return 1
        last_sent = None
        try:
            import fleet_db
            last_sent = last_sent_by_item(fleet_db.connect(Path(a.db_path) if a.db_path else None))
        except Exception as exc:  # noqa: BLE001 -- no bus history: send only the new ones
            print(f"gate_drops intake: no message history, no re-sends: {exc}", file=sys.stderr)
        p = intake_plan(backlog, prod, a.run_id, fetch_parents(backlog, backlog, a.repo), last_sent)
        if not a.dry_run:
            p.update(apply(p, a.repo, a.run_id, db_path=a.db_path))
        summary = {k: p.get(k) for k in ("scanned", "skipped", "results", "sent")}
        summary["eligible"] = len(p["eligible"])
        summary["dropped"] = count_actions(p["dropped"])
        summary["actions"] = len(p["actions"])
        print(json.dumps(summary if not a.dry_run else dict(summary, plan_actions=p["actions"][:50],
                                                           message=p["message"], messages=p["messages"])))
        return 0
    items = fill_capped_comments(load_items(a.items), a.repo)
    p = plan(items, a.run_id, fetch_parents(items, items, a.repo))
    if not a.dry_run:
        p.update(apply(p, a.repo, a.run_id, db_path=a.db_path))
    print(json.dumps(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
