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
(prod DB, secrets, Cloudflare: HQ holds that access, minions don't). A `fleet:priority-low` item
already sent FLEET_GATE_DROP_MAX_SENDS_LOW times gets `fleet:parked` instead of another send
(fleet-kit#1463); the label comes off with needs-spec once it clears the gates.

Usage:
  gate_drops.py run --items /tmp/gru_items.json [--run-id ID] [--repo O/R] [--dry-run]
  gate_drops.py intake [--repo O/R] [--dry-run]
  gate_drops.py count [--hours 6]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quality_gate  # noqa: E402
import vision_link_gate  # noqa: E402
from board_github import LABEL_CLAIMED, LABEL_PROD_ACCESS, NOT_FOR_MINIONS, blocked_by_numbers  # noqa: E402
from items_arg import HELP as ITEMS_HELP, load_items  # noqa: E402
import build_lane_rule  # noqa: E402

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
# intake leaves these alone: in flight, not minion work at all, or tracking-only (epic, ledger).
LEDGER = f"{PREFIX}ledger"
INTAKE_SKIP = {LABEL_CLAIMED, *NOT_FOR_MINIONS, quality_gate.EPIC, LEDGER}
# An item still stuck at a gate this long after marie was last told about it is sent again.
# 2026-09-29: 32 items sat re-dropped every 30 min for 2.5 days with no message. marie fixed one
# gap (the vision-link) and the item stayed labeled, so its next gap (acceptance) was never sent.
RESEND_S = float(os.environ.get("FLEET_GATE_DROP_RESEND_H", "72")) * 3600
# fleet-kit#1463: a priority-low item sent this many times with nobody speccing it is parked
# (label, no more re-sends). 2026-09-30: 8 low items sent twice in 3 days, specced by nobody.
LOW = f"{PREFIX}priority-low"
PARKED = f"{PREFIX}parked"
DEAD_END = f"{PREFIX}dead-end-blocked"
MAX_SENDS_LOW = int(os.environ.get("FLEET_GATE_DROP_MAX_SENDS_LOW", "2"))

# What each gate's drop reason means for the person who has to fix it.
GAPS = (
    ("no Vision-link", "vision-link",
     "a `Vision-link:` line naming a KR id (`python3 /fleet-kit/scripts/okr.py ids`), or `Vision-link: none (maintenance)`"),
    ("no quality: label", "quality-label",
     "exactly one `quality:ship-it` / `quality:solid` / `quality:world-class` label"),
    ("more than one quality label", "quality-label",
     "exactly one `quality:` label (it has several)"),
    ("no Given/When/Then", "acceptance",
     "at least one Given/When/Then acceptance criterion in the body or the newest PRD comment"),
)
BY_DESIGN = ("tracking-only parent", "world-class with no")
# fleet_init.py files a new product's first issues with this marker and a `Blocked by #N` line
# naming the founding issue each one needs first (CI needs a stack; a page needs CI).
FOUNDING_MARKER = "<!-- fleet-founding"


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


def founding_waits(items: list[dict], repo: str | None, run=None) -> dict[int, int]:
    """{item: the still-open issue it waits on} for founding issues only. An empty repo cannot
    take seven builders at once, each picking its own stack. Only an item carrying
    FOUNDING_MARKER costs a gh call, so a board with none behaves exactly as before; a blocker
    that will not load is treated as closed (never hold work on a gh hiccup)."""
    state: dict[int, bool] = {}
    out: dict[int, int] = {}
    for it in items:
        if FOUNDING_MARKER not in (it.get("body") or ""):
            continue
        for n in blocked_by_numbers(it):
            if n not in state:
                state[n] = False
                try:
                    r = (run or _gh)(["gh", "issue", "view", str(n), "--json", "state",
                                      *(["--repo", repo] if repo else [])])
                    state[n] = r.returncode == 0 and json.loads(r.stdout or "{}").get("state") == "OPEN"
                except (subprocess.TimeoutExpired, ValueError):
                    pass
            if state[n]:
                out[it["number"]] = n
                break
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


def refetch_spec_drops(items: list[dict], repo: str | None, run=None) -> list[dict]:
    """Re-read from gh, in place, every item the caller's copy would drop for a spec gap.

    jefe msg#521: `run` trusts the --items file gru builds by hand, and gru-4902 (2026-10-01)
    built one whose comments the gates could not read -- 52 items labeled needs-spec and one
    ask raised to Reif, while the intake run minutes later read the same issues from gh, found
    marie's Vision-link/Given-When-Then comments, and took the label back off 47 of them. A
    needs-spec label is now only ever decided on what GitHub holds, not on the caller's copy.
    """
    probe = plan([dict(it) for it in items], "probe")
    spec = {r["number"] for r in probe["dropped"] if r.get("action") == "needs-spec"}
    for it in items:
        if it["number"] not in spec:
            continue
        try:
            r = (run or _gh)(["gh", "issue", "view", str(it["number"]), "--json",
                              "body,labels,comments", *(["--repo", repo] if repo else [])], 120)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            try:
                fresh = json.loads(r.stdout or "{}")
            except ValueError:
                continue
            it.update({k: fresh[k] for k in ("body", "labels", "comments") if k in fresh})
    return items


def plan(items: list[dict], run_id: str, parents: dict | None = None,
         waits: dict[int, int] | None = None) -> dict:
    """Pure. Decides every label/comment/ask and the final eligible list; runs no gh.
    `parents`: fetch_parents() output, so `Vision-link: #<epic>` inherits the epic's link.
    `waits`: founding_waits() output; those items sit this pass out, by design."""
    waits = waits or {}
    waiting = [it["number"] for it in items if it["number"] in waits]
    items = [it for it in items if it["number"] not in waits]
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
        # Mechanical: filers pre-stamp the default and marie's score adds a second one without
        # removing it (jefe msg#632: 4 of 4 needs-spec items). The scored label wins; two
        # non-default labels stay a real gap.
        quals = set(labels) & set(quality_gate.QUALITY_LABELS)
        if len(quals) == 2 and DEFAULT_QUALITY in quals:
            actions.append({"number": it["number"], "op": "remove_label", "label": DEFAULT_QUALITY})
            fixed.add(it["number"])
            it = dict(it, labels=[x for x in it.get("labels") or []
                                  if (x.get("name") if isinstance(x, dict) else x) != DEFAULT_QUALITY])
        patched.append(it)
    items = patched
    by_num = {it["number"]: it for it in items}
    vis = vision_link_gate.gate_candidates(items, parents)
    survivors = [by_num[n] for n in vis["eligible"]]
    qual = quality_gate.gate_candidates(survivors)

    drops = [dict(d, gate="vision-link") for d in vis["dropped"]]
    drops += [dict(d, gate="quality") for d in qual["dropped"]]
    eligible = list(qual["eligible"])
    records: list[dict] = [{"number": n, "gate": "quality", "reason": "quality label normalized",
                            "action": "fixed"} for n in eligible if n in fixed]
    records += [{"number": n, "gate": "founding-order", "action": "by-design",
                 "reason": f"waits for founding issue #{waits[n]} to close"} for n in waiting]
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


def sends_by_item(conn) -> dict[int, int]:
    """How many gate-drop sends named each item (marie + jefe copies of one send count once)."""
    seen: dict[int, set] = {}
    for items, sent_at in conn.execute("SELECT items, sent_at FROM msgs WHERE kind = 'gate-drop'"):
        try:
            nums = json.loads(items or "[]")
        except ValueError:
            continue
        for n in nums if isinstance(nums, list) else []:
            if isinstance(n, int):
                seen.setdefault(n, set()).add(sent_at)
    return {n: len(v) for n, v in seen.items()}


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
                now: float | None = None, sends: dict[int, int] | None = None,
                waits: dict[int, int] | None = None) -> dict:
    """Pure. plan() over every open backlog item except claimed / human-op / prod-access / epics;
    the Reif ask is replaced by one bus message (an hourly sweep of 400+ items would otherwise
    ask Reif every hour), plus hq's prod-access message."""
    todo = [i for i in backlog if not set(_names(i.get("labels"))) & INTAKE_SKIP]
    p = plan(todo, run_id, parents, waits)
    # A new gap comment counts too: an item already labeled that moved on to its next gap.
    newly = sorted({a["number"] for a in p["actions"] if a["op"] == "comment" or
                    (a["op"] == "add_label" and a["label"] == NEEDS_SPEC)})
    stale = []
    if last_sent is not None:
        now = now or time.time()
        stale = sorted(r["number"] for r in p["dropped"] if r.get("action") == "needs-spec"
                       and r["number"] not in newly and now - last_sent.get(r["number"], 0) >= RESEND_S)
    # Low items already sent MAX_SENDS_LOW times are parked instead of sent again.
    labels_of = {i["number"]: _names(i.get("labels")) for i in todo}
    park = [n for n in stale if LOW in labels_of[n] and (sends or {}).get(n, 0) >= MAX_SENDS_LOW]
    stale = [n for n in stale if n not in park]
    p["actions"] += [{"number": n, "op": "add_label", "label": PARKED}
                     for n in park if PARKED not in labels_of[n]]
    p["actions"] += [{"number": n, "op": "remove_label", "label": PARKED}
                     for n in p["eligible"] if PARKED in labels_of[n]]
    p["parked"] = park
    p["ask"] = None
    p["message"] = intake_message(p["dropped"], newly, run_id, stale)
    p["messages"] = [m for m in (prod_access_message(prod_access),) if m]
    p["scanned"], p["skipped"] = len(backlog), len(backlog) - len(todo)
    return p


INTAKE_FIELDS = "number,title,labels,body,comments,createdAt"


# The whole-backlog read (~550 issues with bodies and comments) is one heavy GraphQL call, and
# GitHub answers it with a 504 or a cut-off body ("unexpected end of JSON input") about one pass
# in three: 133 of 363 intake runs in gru.log failed that way, two in a row on 2026-10-05, so
# five items marie had already fixed kept fleet:needs-spec for 2h+ (jefe msg#834). The same
# read retried a moment later goes through, so try it LIST_OPEN_TRIES times before giving up.
LIST_OPEN_TRIES = int(os.environ.get("FLEET_GATE_DROP_LIST_TRIES", "3"))


def _gh_json(cmd: list[str], what: str, run=None, sleep=time.sleep):
    """Run one gh read, retried LIST_OPEN_TRIES times on a non-zero exit, a timeout or cut-off JSON."""
    err = ""
    for attempt in range(max(1, LIST_OPEN_TRIES)):
        if attempt:
            sleep(10 * attempt)
        try:
            r = (run or _gh)(cmd)
        except subprocess.TimeoutExpired:
            err = "timed out"
            continue
        if r.returncode == 0:
            try:
                return json.loads(r.stdout or "null")
            except ValueError as e:
                err = f"bad JSON: {e}"
                continue
        err = (r.stderr or "")[:200]
    raise RuntimeError(f"{what} failed: {err}")


def list_open(label: str, repo: str | None, fields: str = INTAKE_FIELDS, run=None,
              sleep=time.sleep) -> list[dict]:
    cmd = ["gh", "issue", "list", "--state", "open", "--label", label, "--limit", "2000",
           "--json", fields, *(["--repo", repo] if repo else [])]
    return _gh_json(cmd, f"gh issue list --label {label}", run, sleep) or []


# The retries above did not save the whole-backlog read: after they shipped, 31 of the next 33
# intake runs still failed (gru.log, 2026-10-05: 504s and cut-off JSON), because every retry
# re-asks for ~570 issues with bodies and 100 comments each in one GraphQL answer. So
# needs-spec labels marie had already earned off stayed on (jefe msg#897: #11116, #11176,
# #11239). Read the backlog in pages of PAGE_SIZE instead, each page retried on its own.
PAGE_SIZE = int(os.environ.get("FLEET_GATE_DROP_PAGE_SIZE", "25"))
_PAGE_QUERY = """query($owner:String!,$name:String!,$label:String!,$first:Int!,$after:String){
 repository(owner:$owner,name:$name){issues(states:OPEN,labels:[$label],first:$first,after:$after,
  orderBy:{field:CREATED_AT,direction:ASC}){pageInfo{hasNextPage endCursor}
  nodes{number title body createdAt labels(first:50){nodes{name}}
   comments(last:100){nodes{body createdAt author{login}}}}}}}"""


def _repo_slug(repo: str | None, run=None) -> str:
    if repo:
        return repo
    out = _gh_json(["gh", "repo", "view", "--json", "nameWithOwner"], "gh repo view", run,
                   lambda s: None)
    return out["nameWithOwner"]


def list_open_paged(label: str, repo: str | None, run=None, sleep=time.sleep) -> list[dict]:
    """Same items and shape as list_open(label, INTAKE_FIELDS), read one small page at a time.
    Comments are the NEWEST 100 (list_open got the oldest); the gates read the newest anyway."""
    owner, name = _repo_slug(repo, run).split("/", 1)
    out, after = [], None
    while True:
        cmd = ["gh", "api", "graphql", "-f", f"query={_PAGE_QUERY}", "-f", f"owner={owner}",
               "-f", f"name={name}", "-f", f"label={label}", "-F", f"first={PAGE_SIZE}",
               *(["-f", f"after={after}"] if after else [])]
        data = _gh_json(cmd, f"backlog page after {after or 'start'} (--label {label})", run, sleep)
        issues = data["data"]["repository"]["issues"]
        for n in issues["nodes"]:
            out.append({"number": n["number"], "title": n["title"], "body": n["body"],
                        "createdAt": n["createdAt"],
                        "labels": [{"name": l["name"]} for l in n["labels"]["nodes"]],
                        "comments": [dict(c, author=c.get("author") or {"login": ""})
                                     for c in n["comments"]["nodes"]]})
        if not issues["pageInfo"]["hasNextPage"]:
            return out
        after = issues["pageInfo"]["endCursor"]


# gru step 2b reads the backlog tier by tier. 2026-10-05: in 15 of 36 passes gru stopped after
# the high tier (67 open) and never read medium (189) or low (200), leaving 36-87% of the hour
# unspent. One call returns every tier in the order the packer cuts, so there is no next read.
# The order comes from a light list (labels only); bodies are read per item for the first
# --first of it, because the whole-backlog body read failed 3 of 3 tries on 2026-10-05.
CANDIDATE_LIST_FIELDS = "number,labels,createdAt"
CANDIDATE_FIELDS = "number,title,labels,body,comments,createdAt"
TIERS = (f"{PREFIX}priority-high", f"{PREFIX}priority-medium", LOW)


BUILT_HOURS = 24.0
_PR_REF = re.compile(r"\b(?:Part of|Closes|Fixes|Resolves|Built in|Builds)\s+#(\d+)", re.IGNORECASE)


def built_by_prs(prs: list[dict], now: float, hours: float = BUILT_HOURS) -> dict[int, str]:
    """Pure. item -> why, for every item an open PR or one merged in the last `hours` already
    builds (branch item ids, or 'Part of #N' in the body). 2026-10-08: the hour's one minion
    slot went three times to items like that (#11634 under open #11741; #11712/#11713 under
    #11739, merged 20 min earlier and waiting on deploy and the prod walk before the issue
    closes); each minion read the PRs, built nothing and burned the slot."""
    import red_prs
    out: dict[int, str] = {}
    for pr in prs:
        state = (pr.get("state") or "").upper()
        if state == "OPEN":
            why = f"PR #{pr.get('number')} open"
        elif state == "MERGED":
            t = _ts(pr.get("mergedAt") or "")
            if t is None or now - t > hours * 3600:
                continue
            why = f"PR #{pr.get('number')} merged {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(t))}"
        else:
            continue
        nums = set(red_prs.items_of(pr.get("headRefName") or ""))
        nums |= {int(m) for m in _PR_REF.findall(pr.get("body") or "")}
        for n in nums:
            out.setdefault(n, why)
    return out


TRIED_HOURS = 24.0


def tried_by_runs(runs: list[dict], now: float, hours: float = TRIED_HOURS) -> dict[int, str]:
    """Pure. item -> why, for every item a minion already tried in the last `hours` and left
    with no commit and no PR (status ok or quiet, commits 0, no checkpoint_pr). 2026-10-08,
    after built_by_prs landed: the hour's one slot went to #10032+#10034, which two minions had
    already read that day and dropped as 'too wide, needs slicing'; the third read the same and
    opened nothing. A re-read a few hours later finds the same item; marie must slice it first."""
    out: dict[int, str] = {}
    import red_prs
    for r in runs:
        if (r.get("member") or "") != "minion" or (r.get("kind") or "llm") != "llm":
            continue
        if (r.get("status") or "") not in ("ok", "quiet"):
            continue
        if (r.get("commits") or 0) or r.get("checkpoint_pr") or r.get("pr"):
            continue
        t = r.get("recorded_at")
        if not isinstance(t, (int, float)) or now - t > hours * 3600:
            continue
        why = f"minion tried {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(t))}, no commit, no PR"
        for n in red_prs.items_of(f"minion-item{r.get('item_id') or ''}-0-0"):
            out.setdefault(n, why)
    return out


def fetch_minion_runs(now: float, hours: float = TRIED_HOURS, db_path=None) -> list[dict]:
    """Minion llm runs that ended in the last `hours`, from fleet.db. Fails open ([])."""
    try:
        import fleet_db
        conn = fleet_db.connect(Path(db_path) if db_path else None)
        rows = conn.execute(
            "select member, kind, item_id, status, commits, checkpoint_pr, pr, recorded_at from runs "
            "where member='minion' and recorded_at > ?", (now - hours * 3600,)).fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001 -- a missing or locked db must not stop the pass
        print(f"gate_drops candidates: runs read failed ({exc}); tried items not dropped", file=sys.stderr)
        return []
    keys = ("member", "kind", "item_id", "status", "commits", "checkpoint_pr", "pr", "recorded_at")
    return [dict(zip(keys, row)) for row in rows]


def _ts(iso: str) -> float | None:
    try:
        return datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def fetch_prs_touching(repo: str | None, now: float, run=None) -> list[dict]:
    """Open PRs plus those merged in the last day, light fields, one gh call. Fails open ([])."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - BUILT_HOURS * 3600))
    cmd = ["gh", "pr", "list", "--state", "all", "--limit", "200", "--search", f"updated:>={since}",
           "--json", "number,state,headRefName,body,mergedAt"]
    if repo:
        cmd += ["--repo", repo]
    try:
        r = (run or _gh)(cmd)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"gate_drops candidates: PR read failed ({exc}); built items not dropped", file=sys.stderr)
        return []
    if r.returncode != 0:
        print(f"gate_drops candidates: PR read failed ({(r.stderr or '').strip()[:120]}); built items not dropped",
              file=sys.stderr)
        return []
    try:
        return json.loads(r.stdout or "[]")
    except ValueError:
        return []


def candidates(backlog: list[dict], only_labels: list[str] | None = None,
               built: dict[int, str] | None = None) -> list[dict]:
    """Pure. Open backlog items gru may build, high -> medium -> low -> unranked, oldest first.
    `only_labels` (FLEET_BUILD_ONLY_LABELS, 2026-10-08): when given, an item must carry one of
    them or it is not a candidate at all -- gru never claims it. See build_lane_rule.py.
    `built` (built_by_prs, and tried_by_runs): items a PR already builds, or a minion already
    tried today and left with no commit and no PR, are not candidates either."""
    skip = INTAKE_SKIP | {PARKED}
    want = set(only_labels or ())
    held = set(built or {})

    def tier(i):
        names = _names(i.get("labels"))
        return next((k for k, t in enumerate(TIERS) if t in names), len(TIERS))
    todo = [i for i in backlog if not set(_names(i.get("labels"))) & skip
            and (not want or want & set(_names(i.get("labels"))))
            and i.get("number") not in held]
    return sorted(todo, key=lambda i: (tier(i), i.get("createdAt") or ""))


# 2026-10-07: 77 of the 80 items `candidates` read were already `dead-end-blocked` (oldest
# first puts them in front), so gru's dead-end gate emptied the set and 28 of ~30 passes
# reported `binding: candidates_exhausted` with 456 items never read. Fresh items fill the read
# first; up to RECHECK labelled ones still ride along so a dead end whose count expired can clear.
RECHECK = 10


def read_slice(todo: list[dict], first: int, recheck: int = RECHECK) -> list[dict]:
    """Pure. The `first` items to read in full: fresh ones in pack order, then labelled dead ends."""
    dead = [i for i in todo if DEAD_END in _names(i.get("labels"))]
    fresh = [i for i in todo if DEAD_END not in _names(i.get("labels"))]
    keep = fresh[:max(first - min(recheck, len(dead)), 0)]
    return keep + dead[:first - len(keep)]


def hydrate(numbers: list[int], repo: str | None, run=None, workers: int = 8) -> list[dict]:
    """Full {body, comments, ...} for each number, same order; an unreadable one is left out."""
    from concurrent.futures import ThreadPoolExecutor

    def one(n):
        cmd = ["gh", "issue", "view", str(n), "--json", CANDIDATE_FIELDS, *(["--repo", repo] if repo else [])]
        for _ in range(2):
            try:
                r = (run or _gh)(cmd)
                if r.returncode == 0:
                    return json.loads(r.stdout)
            except (subprocess.TimeoutExpired, ValueError):
                pass
        print(f"gate_drops candidates: #{n} unreadable, left out", file=sys.stderr)
        return None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [i for i in ex.map(one, numbers) if i]


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
    k = sub.add_parser("candidates", help="every buildable backlog item, all tiers, in pack order (gru step 2b)")
    k.add_argument("--out", required=True, help="write the ordered items here, for `run --items <file>`")
    k.add_argument("--first", type=int, default=80, help="read full text for this many, in order")
    k.add_argument("--repo", default=None)
    c = sub.add_parser("count", help="distinct items dropped in the last N hours")
    c.add_argument("--hours", type=float, default=6.0)
    a = ap.parse_args(argv)
    if a.cmd == "count":
        print(json.dumps(count(a.hours)))
        return 0
    if a.cmd == "candidates":
        try:
            backlog = list_open(f"{PREFIX}backlog", a.repo, CANDIDATE_LIST_FIELDS)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"gate_drops candidates: {exc}", file=sys.stderr)
            return 1
        only = build_lane_rule.allowed_labels()
        built = built_by_prs(fetch_prs_touching(a.repo, time.time()), time.time())
        tried = tried_by_runs(fetch_minion_runs(time.time()), time.time())
        held = {**tried, **built}
        todo = candidates(backlog, only, held)
        built_dropped = {i["number"]: built[i["number"]] for i in candidates(backlog, only) if i["number"] in built}
        tried_dropped = {i["number"]: tried[i["number"]] for i in candidates(backlog, only)
                         if i["number"] in tried and i["number"] not in built}
        full = hydrate([i["number"] for i in read_slice(todo, a.first)], a.repo)
        Path(a.out).write_text(json.dumps(fill_capped_comments(full, a.repo)))
        tiers = [t.rsplit("-", 1)[-1] for t in TIERS] + ["unranked"]
        by = {t: [] for t in tiers}
        for i in todo:
            names = _names(i.get("labels"))
            by[next((t for t, full in zip(tiers, TIERS) if full in names), "unranked")].append(i["number"])
        print(json.dumps({"out": a.out, "scanned": len(backlog), "skipped": len(backlog) - len(todo),
                          "lane_rule": only, "lane_rule_dropped": (len(candidates(backlog)) - len(candidates(backlog, only))) if only else 0,
                          "built_dropped": built_dropped, "tried_dropped": tried_dropped,
                          "by_tier": {t: len(v) for t, v in by.items()}, "written": len(full),
                          "numbers": [i["number"] for i in full], "beyond_first": len(todo) - len(full)}))
        return 0
    if a.cmd == "intake":
        try:
            backlog = list_open_paged(f"{PREFIX}backlog", a.repo, run=lambda c: _gh(c, 120))
            prod = list_open(LABEL_PROD_ACCESS, a.repo, "number,title,labels")
            fill_capped_comments(backlog, a.repo)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"gate_drops intake: {exc}", file=sys.stderr)
            return 1
        last_sent = sends = None
        try:
            import fleet_db
            conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
            last_sent, sends = last_sent_by_item(conn), sends_by_item(conn)
        except Exception as exc:  # noqa: BLE001 -- no bus history: send only the new ones
            print(f"gate_drops intake: no message history, no re-sends: {exc}", file=sys.stderr)
        p = intake_plan(backlog, prod, a.run_id, fetch_parents(backlog, backlog, a.repo), last_sent,
                        sends=sends, waits=founding_waits(backlog, a.repo))
        if not a.dry_run:
            p.update(apply(p, a.repo, a.run_id, db_path=a.db_path))
        summary = {k: p.get(k) for k in ("scanned", "skipped", "results", "sent", "parked")}
        summary["eligible"] = len(p["eligible"])
        summary["dropped"] = count_actions(p["dropped"])
        summary["actions"] = len(p["actions"])
        print(json.dumps(summary if not a.dry_run else dict(summary, plan_actions=p["actions"][:50],
                                                           message=p["message"], messages=p["messages"])))
        return 0
    items = refetch_spec_drops(fill_capped_comments(load_items(a.items), a.repo), a.repo)
    p = plan(items, a.run_id, fetch_parents(items, items, a.repo), founding_waits(items, a.repo))
    if not a.dry_run:
        p.update(apply(p, a.repo, a.run_id, db_path=a.db_path))
    print(json.dumps(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
