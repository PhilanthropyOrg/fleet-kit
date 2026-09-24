#!/usr/bin/env python3
"""issue_cluster -- one definition of "the same issue", used at birth AND after the fact.

THE PROBLEM (measured 2026-09-24). Over 14 days the board took ~130 new issues/day against ~50
merged PRs/day, and 334 sat open. Most closes were dupes or supersedes, not shipped fixes. Two
shapes dominate the open board: one filer re-filing the same failure with a different number
in the title ("prod alert [pg_lock:3973044:pg_toast_16627]", "[pg_lock:3968749:...]", ...), and
one check filing the same root cause once per member ("gru's reports have no plain-English
words", "nerd's reports have ...", nine of them). A minion then spends a whole pass per twin.

ONE KEY, TWO USES.
  * Dedupe at birth -- `file` (CLI) / `find_existing()` (library): every filer searches the open
    board and its megas for the same signature before creating; a hit becomes a comment on the
    existing item, never a twin. Filer members have raw `gh issue create` denied in their
    fleet.json so this helper is the only door (Reif, 2026-09-24).
  * Mega issues after the fact -- `megas` (CLI) / `plan_megas()`: marie's Part B2 folds open
    issues sharing a signature AND a lane into ONE `fleet:mega` issue with a checklist of child
    refs, and closes each child "tracked in #M". A minion takes the mega as one batch item.

THE SIGNATURE IS THE ROOT-CAUSE PROXY, AND IT IS DELIBERATELY NARROW. Two titles are the same
issue only when they are equal after dropping what varies between firings of ONE failure:
numbers, parentheticals, and a leading "<member>'s " subject. Anything else -- different
component, different lock target, different page -- is a different signature and is never
merged. A missed cluster costs one extra minion item; a wrong merge closes a real, different
bug. Recall is traded for precision on purpose.

REIF'S ASKS ARE NEVER FOLDED. An item labelled fleet:reif-asked / fleet:reif-priority, or an
`ask #N` title, is Reif's own words. It is linked from a mega (so the minion sees it) but never
closed by it, and `file` never turns a new ask into a comment on someone else's issue.

Pure core (signature, find_existing, plan_megas, mega_body) + thin gh seam + CLI, same split as
fanout.py / cost_bridge.py. Standard library only (runs on the fleet host as-is).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

MEGA_LABEL = "fleet:mega"
PROTECTED_LABELS = {"fleet:reif-asked", "fleet:reif-priority"}
# Never folded into a mega: in flight (a minion owns it), already an aggregate, or queued to
# ride an open PR (Part C5) -- moving any of those would orphan work already under way.
SKIP_LABELS = {"fleet:claimed", "fleet:epic", MEGA_LABEL, "fleet:fold-into-pr"}
SIG_MARKER = "mega-signature:"
LANE_MARKER = "mega-lane:"
DEFAULT_MIN_CLUSTER = 3
# Members whose prompts file issues. Each denies raw `Bash(gh issue create:*)` in its
# fleet.json and files through `issue_cluster.py file` instead (test_issue_cluster.py pins it).
# Script filers call file_issue()/find_existing() directly: board_github.file_item (judge-judy),
# inbox's alert path, reif_eyes. journey_issue_filer / prod_incident keep their own marker dedupe.
FILER_MEMBERS = ["sentry", "red", "nerd", "the-fixer", "librarian", "vp",
                 "dont-shoot-the-messenger", "marie"]


def _labels(issue: dict) -> set[str]:
    out = set()
    for lab in issue.get("labels") or []:
        out.add(lab.get("name") if isinstance(lab, dict) else str(lab))
    return {x for x in out if x}


def signature(title: str) -> str:
    """Normalise a title to what stays constant across firings of ONE failure."""
    t = (title or "").strip().lower()
    t = re.sub(r"^\[mega[^\]]*\]\s*", "", t)            # a mega's own title prefix
    t = re.sub(r"^[\w.-]+'s\s+", "<who>'s ", t)          # "gru's reports ..." == "nerd's reports ..."
    t = re.sub(r"\([^)]*\)", " ", t)                     # "(11 of 11 finished runs in 24h)"
    t = re.sub(r"\b(?:gh|fk)?#\d+", "#", t)
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"[\s\-—–,.;!?\"`]+", " ", t)
    return t.strip()


def lane(issue: dict) -> str:
    for name in sorted(_labels(issue)):
        if name.startswith("lane:"):
            return name
    return ""


def area(issue: dict) -> str:
    """The batching key for fanout.pack_batches: the lane label (same module/owner family), or
    "" when the item has none. Coarser than signature on purpose -- items that merely share a
    lane still share worktree context, tests and reviewers, which is what amortizes."""
    return lane(issue)


def is_protected(issue: dict) -> bool:
    if _labels(issue) & PROTECTED_LABELS:
        return True
    return bool(re.match(r"^\s*ask #\d+", issue.get("title") or "", re.I))


def _mega_key(issue: dict) -> tuple[str, str] | None:
    body = issue.get("body") or ""
    sig = re.search(rf"^{re.escape(SIG_MARKER)}\s*(.+)$", body, re.M)
    if not sig:
        return None
    ln = re.search(rf"^{re.escape(LANE_MARKER)}\s*(.*)$", body, re.M)
    return sig.group(1).strip(), (ln.group(1).strip() if ln else "")


def find_existing(title: str, labels: list[str] | None, open_issues: list[dict]) -> dict | None:
    """The open issue a new filing with this title belongs to, or None. A mega for the same
    signature wins over a plain twin (it is where the work is tracked). Lane must agree when
    both sides name one -- same words in two different lanes is two different problems."""
    sig = signature(title)
    if not sig:
        return None
    want_lane = next((x for x in (labels or []) if x.startswith("lane:")), "")

    def lane_ok(other: str) -> bool:
        return not want_lane or not other or other == want_lane

    for it in open_issues:
        if MEGA_LABEL in _labels(it):
            key = _mega_key(it)
            if key and key[0] == sig and lane_ok(key[1]):
                return it
    for it in open_issues:
        if MEGA_LABEL not in _labels(it) and signature(it.get("title") or "") == sig and lane_ok(lane(it)):
            return it
    return None


def plan_megas(open_issues: list[dict], min_size: int = DEFAULT_MIN_CLUSTER) -> list[dict]:
    """Group open issues by (signature, lane) into mega plans. Pure.

    Returns [{"signature", "lane", "mega": <existing mega number or None>, "children": [issue,
    ...], "linked": [protected issue, ...]}]. A NEW mega needs `min_size` foldable children;
    an EXISTING mega for the key absorbs any new child. Groups are keyed on the exact
    signature, so two different root causes can never share a plan."""
    megas: dict[tuple[str, str], dict] = {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in open_issues:
        labs = _labels(it)
        if MEGA_LABEL in labs:
            key = _mega_key(it)
            if key:
                megas.setdefault(key, it)
            continue
        if labs & SKIP_LABELS:
            continue
        sig = signature(it.get("title") or "")
        if sig:
            groups.setdefault((sig, lane(it)), []).append(it)

    plans = []
    for key, members in groups.items():
        children = sorted((m for m in members if not is_protected(m)), key=lambda m: m["number"])
        linked = sorted((m for m in members if is_protected(m)), key=lambda m: m["number"])
        existing = megas.get(key)
        if existing:
            if children:
                plans.append({"signature": key[0], "lane": key[1], "mega": existing["number"],
                              "children": children, "linked": linked})
        elif len(children) >= min_size:
            plans.append({"signature": key[0], "lane": key[1], "mega": None,
                          "children": children, "linked": linked})
    plans.sort(key=lambda p: -len(p["children"]))
    return plans


_PRIORITY_ORDER = ["fleet:priority-high", "fleet:priority-medium", "fleet:priority-low"]


def mega_labels(children: list[dict]) -> list[str]:
    labs = set().union(*(_labels(c) for c in children)) if children else set()
    out = ["fleet:backlog", MEGA_LABEL]
    ln = lane(children[0]) if children else ""
    if ln:
        out.append(ln)
    for p in _PRIORITY_ORDER:
        if p in labs:
            out.append(p)
            break
    cx = [int(m.group(1)) for x in labs for m in [re.match(r"fleet:complexity-(\d+)$", x)] if m]
    if cx:
        # One root cause, N instances: sized as the biggest instance, not the sum.
        out.append(f"fleet:complexity-{max(cx)}")
    return out


def mega_title(plan: dict) -> str:
    return f"[mega x{len(plan['children'])}] {plan['children'][0].get('title', '').strip()}"[:250]


def checklist_line(it: dict) -> str:
    return f"- [ ] #{it['number']} {(it.get('title') or '').strip()}"


def mega_body(plan: dict) -> str:
    lines = [
        f"Mega issue: {len(plan['children'])} open issues share one root cause (same failure "
        "signature, same lane). Build it as ONE item: fix the shared cause once, then tick every "
        "box below with the evidence that covers it. `Closes` this mega only when every box is ticked.",
        "",
        f"{SIG_MARKER} {plan['signature']}",
        f"{LANE_MARKER} {plan['lane']}",
        "",
        "## Children (closed as tracked here)",
        *[checklist_line(c) for c in plan["children"]],
    ]
    if plan.get("linked"):
        lines += ["", "## Linked Reif asks (kept open -- close each only with its own evidence)",
                  *[f"- #{c['number']} {(c.get('title') or '').strip()}" for c in plan["linked"]]]
    lines += ["", "Filed by marie Part B2 (scripts/issue_cluster.py megas)."]
    return "\n".join(lines)


def extend_mega_body(body: str, plan: dict) -> str:
    have = set(int(n) for n in re.findall(r"#(\d+)", body or ""))
    add = [checklist_line(c) for c in plan["children"] if c["number"] not in have]
    link = [f"- #{c['number']} {(c.get('title') or '').strip()} (Reif ask, kept open)"
            for c in plan.get("linked") or [] if c["number"] not in have]
    return (body or "").rstrip() + ("\n" + "\n".join(add + link) if add or link else "")


# ---------------------------------------------------------------- gh seam


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=90)


def _repo_args(repo: str | None) -> list[str]:
    return ["--repo", repo] if repo else []


def list_open(repo: str | None, run=_run, limit: int = 1000) -> list[dict]:
    r = run(["gh", "issue", "list", *_repo_args(repo), "--state", "open", "--limit", str(limit),
             "--json", "number,title,labels,body"])
    if r.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {(r.stderr or r.stdout or '').strip()[:200]}")
    return json.loads(r.stdout or "[]")


def file_issue(title: str, body: str, labels: list[str], repo: str | None = None,
               run=_run, open_issues: list[dict] | None = None, who: str = "") -> dict:
    """The one door for filing: comment on the existing twin/mega, or create. Reif asks are
    always created (never folded into someone else's item). A failed board read still files:
    a twin beats a dropped finding, and the failure is surfaced in the result."""
    lookup_error = None
    if open_issues is None:
        try:
            open_issues = list_open(repo, run)
        except (RuntimeError, ValueError) as exc:
            open_issues, lookup_error = [], str(exc)
    protected = bool(set(labels) & PROTECTED_LABELS)
    hit = None if protected else find_existing(title, labels, open_issues)
    if hit:
        note = f"Seen again{f' by {who}' if who else ''}: {title}\n\n{(body or '').strip()[:1500]}"
        r = run(["gh", "issue", "comment", str(hit["number"]), *_repo_args(repo), "--body", note])
        return {"action": "commented", "number": hit["number"], "ok": r.returncode == 0}
    cmd = ["gh", "issue", "create", *_repo_args(repo), "--title", title, "--body", body]
    for lab in labels:
        cmd += ["--label", lab]
    r = run(cmd)
    out = {"action": "created", "ok": r.returncode == 0,
           "url": (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else None}
    if lookup_error:
        out["dedupe_lookup_error"] = lookup_error
    if r.returncode != 0:
        out["error"] = (r.stderr or r.stdout or "").strip()[:300]
    return out


def apply_plan(plan: dict, repo: str | None = None, run=_run) -> dict:
    """Create (or extend) the mega, THEN close children -- a child is never closed unless the
    mega that tracks it exists. Linked Reif asks only get a pointer comment."""
    if plan["mega"] is None:
        cmd = ["gh", "issue", "create", *_repo_args(repo), "--title", mega_title(plan),
               "--body", mega_body(plan)]
        for lab in mega_labels(plan["children"]):
            cmd += ["--label", lab]
        r = run(cmd)
        m = re.search(r"/issues/(\d+)", r.stdout or "")
        if r.returncode != 0 or not m:
            return {"ok": False, "error": (r.stderr or r.stdout or "").strip()[:300], "closed": []}
        mega = int(m.group(1))
    else:
        mega = plan["mega"]
        r = run(["gh", "issue", "view", str(mega), *_repo_args(repo), "--json", "body"])
        body = json.loads(r.stdout or "{}").get("body", "") if r.returncode == 0 else None
        if body is None:
            return {"ok": False, "error": f"could not read mega #{mega}", "closed": []}
        run(["gh", "issue", "edit", str(mega), *_repo_args(repo), "--body", extend_mega_body(body, plan)])
    closed = []
    for c in plan["children"]:
        r = run(["gh", "issue", "close", str(c["number"]), *_repo_args(repo), "--reason", "not planned",
                 "--comment", f"Tracked in #{mega} (mega issue: same root cause, one build). "
                              "Reopen if this turns out to be a different cause."])
        if r.returncode == 0:
            closed.append(c["number"])
    for c in plan.get("linked") or []:
        run(["gh", "issue", "comment", str(c["number"]), *_repo_args(repo), "--body",
             f"Related mega #{mega} tracks the same root cause. This ask stays open on its own."])
    return {"ok": True, "mega": mega, "created": plan["mega"] is None, "closed": closed,
            "linked": [c["number"] for c in plan.get("linked") or []]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Dedupe at birth (file) and fold twins into mega issues (megas).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("file", help="file an issue, or comment on the open twin/mega if one exists")
    f.add_argument("--title", required=True)
    f.add_argument("--body", default="")
    f.add_argument("--body-file")
    f.add_argument("--label", action="append", default=[], help="repeatable; comma lists allowed")
    f.add_argument("--repo")
    m = sub.add_parser("megas", help="fold open same-signature issues into mega issues")
    m.add_argument("--repo")
    m.add_argument("--min-size", type=int, default=DEFAULT_MIN_CLUSTER)
    m.add_argument("--max", type=int, default=10, help="most megas to create/extend this pass")
    m.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("signature", help="print a title's signature")
    s.add_argument("title")
    a = ap.parse_args(argv)

    if a.cmd == "signature":
        print(signature(a.title))
        return 0
    if a.cmd == "file":
        body = open(a.body_file).read() if a.body_file else a.body
        labels = [x.strip() for lab in a.label for x in lab.split(",") if x.strip()]
        res = file_issue(a.title, body, labels, repo=a.repo, who=os.environ.get("FLEET_MEMBER", ""))
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    plans = plan_megas(list_open(a.repo), min_size=a.min_size)[: a.max]
    if a.dry_run:
        print(json.dumps([{"signature": p["signature"], "lane": p["lane"], "mega": p["mega"],
                           "children": [c["number"] for c in p["children"]],
                           "linked": [c["number"] for c in p["linked"]]} for p in plans], indent=2))
        return 0
    results = [apply_plan(p, repo=a.repo) for p in plans]
    print(json.dumps({"megas_created": sum(1 for r in results if r.get("created")),
                      "megas_extended": sum(1 for r in results if r.get("ok") and not r.get("created")),
                      "children_closed": sum(len(r.get("closed") or []) for r in results),
                      "results": results}, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
