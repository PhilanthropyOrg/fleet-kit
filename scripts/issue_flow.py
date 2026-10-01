#!/usr/bin/env python3
"""issue_flow -- who files, who closes, and what on the open board can be closed without a build.

MEASURED 2026-10-01 on the product board (14 days, every issue read with its close event):
2,213 issues opened, 1,796 closed, 575 open, +30 a day. Three facts shaped this file.

  * Nobody could say who files. Every issue has the same GitHub author (the fleet's one token),
    so the source had to be guessed from title shapes. `source_of()` reads the `Filed-by:` line
    the filing tools now stamp (issue_cluster.stamp) and falls back to those shapes for the rest.
  * Only 1 close in 3 was real code (a merged PR, linked or cited). 18% were "fix: PR #N failed
    code review" / "the-fixer: PR #N is red" items closed by hand, one model turn each, after
    their PR had merged anyway; 22% were twins closed by hand. `prune` lists both in one read.
  * marie's Part B walked `gh issue list --limit 200` one issue at a time, so a 575-item board
    never got a whole pass. `prune` reads the board and the merged PRs once and prints the short
    list worth her turns: PR items whose PR is finished, open twins, and issues a merged PR says
    it closed.

`prune` and `flow` only READ. marie closes (Part B); dont-shoot-the-messenger reports `flow`.
Pure core + thin gh seam + CLI, same split as issue_cluster.py. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issue_cluster as ic  # noqa: E402

UNSTAMPED = "unstamped"
_FILED_BY_RE = re.compile(rf"(?im)^{re.escape(ic.FILED_BY)}[ \t]*([\w.'-]+)")
_MARKER_RE = re.compile(r"<!--\s*fleet:([\w-]+) key=(\S+)\s*-->")
# "fix: PR #9057 failed code review -- ...", "the-fixer: PR #9802 is red on ...": an item ABOUT a PR.
_PR_ITEM_RE = re.compile(r"^\s*[\w-]+:\s*PR\s*#(\d+)\b", re.I)
# Title shapes of the kit's own filers, for issues filed before the stamp existed (and for raw
# `gh issue create` calls, which no tool can stamp). First match wins.
_SHAPES = [
    (re.compile(r"^prod alert \[", re.I), "prod-alert"),
    (re.compile(r"^fix:\s*PR\s*#\d+ failed code review", re.I), "judge-judy"),
    (re.compile(r"^the-fixer:", re.I), "the-fixer"),
    (re.compile(r"^prod-runtime:", re.I), "prod-runtime"),
    (re.compile(r"^ask #\d+", re.I), "ask"),
    (re.compile(r"^\[mega\b", re.I), "marie"),
]


def _labels(issue: dict) -> set[str]:
    return ic._labels(issue)


def source_of(issue: dict) -> str:
    """Who filed this issue: its `Filed-by:` stamp, else the filer its shape gives away, else
    `unstamped` (a model's raw create, or a person)."""
    body = issue.get("body") or ""
    m = _FILED_BY_RE.search(body)
    if m:
        return m.group(1).lower()
    title = issue.get("title") or ""
    for rx, name in _SHAPES:
        if rx.search(title):
            return name
    m = _MARKER_RE.search(body)
    if m:
        return {"sentry-journey": "sentry", "red-team": "red"}.get(m.group(1), m.group(1))
    if "<!-- reif-eyes:" in body:
        return "reif-eyes"
    origin = sorted(x for x in _labels(issue) if x.startswith("origin:"))
    if origin:
        return origin[0].split(":", 1)[1]
    return UNSTAMPED


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def flow(issues: list[dict], now: float, hours: float = 24.0) -> dict:
    """Opened vs closed per source in the window. `issues` rows carry created_at, closed_at and
    state_reason (GitHub REST names). `not_built` = closed as not planned or duplicate."""
    since = now - hours * 3600
    rows: dict[str, dict] = {}
    for it in issues:
        src = source_of(it)
        row = rows.setdefault(src, {"source": src, "opened": 0, "closed": 0, "not_built": 0})
        made, shut = _ts(it.get("created_at")), _ts(it.get("closed_at"))
        if made is not None and made >= since:
            row["opened"] += 1
        if shut is not None and shut >= since:
            row["closed"] += 1
            if (it.get("state_reason") or "").lower() in ("not_planned", "duplicate"):
                row["not_built"] += 1
    out = sorted((r for r in rows.values() if r["opened"] or r["closed"]),
                 key=lambda r: (-r["opened"], -r["closed"], r["source"]))
    for r in out:
        r["net"] = r["opened"] - r["closed"]
    total = {k: sum(r[k] for r in out) for k in ("opened", "closed", "not_built", "net")}
    return {"window_hours": hours, **total, "sources": out}


def flow_line(f: dict, top: int = 6) -> str:
    """The one plain line a report carries."""
    if not f.get("sources"):
        return "Board: nothing opened or closed."
    parts = [f"{r['source']} +{r['opened']}/-{r['closed']}" for r in f["sources"][:top]]
    return (f"Board, last {f['window_hours']:g}h: {f['opened']} opened, {f['closed']} closed "
            f"({f['not_built']} without a build), net {f['net']:+d}. By filer: " + ", ".join(parts))


def _twin_key(issue: dict) -> str:
    m = _MARKER_RE.search(issue.get("body") or "")
    if m:
        return f"marker:{m.group(1)}:{m.group(2)}"
    title = issue.get("title") or ""
    m = _PR_ITEM_RE.match(title)
    if m:  # signature() drops numbers; two items about two different PRs are not twins
        return f"pr:{m.group(1)}:{ic.signature(title)}"
    return ic.signature(title)


def prune_candidates(open_issues: list[dict], merged_prs: list[dict],
                     closed_prs: set[int] | None = None) -> dict:
    """Pure. The open issues worth marie's turns, each with the evidence that makes it one:

      pr_done  an item ABOUT a PR ("fix: PR #N failed code review") whose PR has merged or
               closed. Machine-filed, so listed even when a filer put fleet:reif-priority on it.
      twins    2+ open issues with one signature (or one journey marker) and no lane conflict.
               `keep` is a protected/claimed one if any, else the oldest; megas, epics, Reif's
               asks and claimed items are never in `close`.
      shipped  a merged PR declared `Closes #N` and N is still open -- verify, then close.
    """
    merged = {int(p["number"]): p for p in merged_prs}
    closed_prs = closed_prs or set()
    pr_done, shipped, used = [], [], set()
    for it in sorted(open_issues, key=lambda i: i["number"]):
        m = _PR_ITEM_RE.match(it.get("title") or "")
        pr = int(m.group(1)) if m else None
        if pr in merged or pr in closed_prs:
            state = "merged" if pr in merged else "closed"
            pr_done.append({"number": it["number"], "title": it.get("title", ""), "pr": pr,
                            "reason": f"PR #{pr} {state}; nothing left to fix here"})
            used.add(it["number"])
    closers: dict[int, list[int]] = {}
    for p in merged_prs:
        for ref in p.get("closingIssuesReferences") or []:
            closers.setdefault(int(ref["number"]), []).append(int(p["number"]))
    for it in sorted(open_issues, key=lambda i: i["number"]):
        if it["number"] in closers and it["number"] not in used:
            prs = sorted(closers[it["number"]])
            shipped.append({"number": it["number"], "title": it.get("title", ""), "prs": prs,
                            "reason": "merged PR " + ", ".join(f"#{n}" for n in prs)
                                      + " says it closes this; read the newest comment first"})
    groups: dict[str, list[dict]] = {}
    for it in open_issues:
        labs = _labels(it)
        if it["number"] in used or labs & {ic.MEGA_LABEL, "fleet:epic"}:
            continue
        key = _twin_key(it)
        if key:
            groups.setdefault(key, []).append(it)
    twins = []
    for members in groups.values():
        if len(members) < 2 or len({ic.lane(m) for m in members} - {""}) > 1:
            continue
        members.sort(key=lambda i: i["number"])
        held = [m for m in members if ic.is_protected(m) or "fleet:claimed" in _labels(m)]
        keep = (held or members)[0]
        close = [m["number"] for m in members if m is not keep and m not in held]
        if close:
            twins.append({"keep": keep["number"], "title": keep.get("title", ""), "close": close,
                          "reason": f"duplicate of #{keep['number']}, keeping that one"})
    twins.sort(key=lambda t: -len(t["close"]))
    return {"open": len(open_issues),
            "counts": {"pr_done": len(pr_done), "twins": sum(len(t["close"]) for t in twins),
                       "shipped": len(shipped)},
            "pr_done": pr_done, "twins": twins, "shipped": shipped}


# ---------------------------------------------------------------- gh seam


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def _json(cmd: list[str], run=_run):
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed: {(r.stderr or r.stdout or '').strip()[:200]}")
    return json.loads(r.stdout or "[]")


def read_prune(repo: str | None, run=_run, pr_limit: int = 500) -> dict:
    """Three reads, whatever the board's size: open issues, merged PRs, closed-unmerged PRs."""
    rep = ic._repo_args(repo)
    open_issues = ic.list_open(repo, run, limit=2000)
    merged = _json(["gh", "pr", "list", *rep, "--state", "merged", "--limit", str(pr_limit),
                    "--json", "number,closingIssuesReferences"], run)
    shut = _json(["gh", "pr", "list", *rep, "--state", "closed", "--limit", str(pr_limit),
                  "--json", "number"], run)
    return prune_candidates(open_issues, merged, {int(p["number"]) for p in shut})


_JQ = ('.[] | select(.pull_request | not) | {number, title, body, state_reason, created_at, '
       'closed_at, labels: [.labels[].name]}')


def read_flow(repo: str | None, hours: float = 24.0, run=_run, now: float | None = None) -> dict:
    """Every issue touched in the window (one paginated REST read), folded by flow()."""
    now = now if now is not None else _dt.datetime.now(_dt.timezone.utc).timestamp()
    since = _dt.datetime.fromtimestamp(now - hours * 3600, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = run(["gh", "api", "--paginate", "-X", "GET", f"repos/{repo or '{owner}/{repo}'}/issues",
             "-f", "state=all", "-f", f"since={since}", "-f", "per_page=100", "--jq", _JQ])
    if r.returncode != 0:
        raise RuntimeError(f"gh api issues failed: {(r.stderr or r.stdout or '').strip()[:200]}")
    rows = [json.loads(line) for line in (r.stdout or "").splitlines() if line.strip()]
    return flow(rows, now, hours)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Board flow per filer, and what can be closed without a build.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prune", help="list close candidates on the open board (reads only)")
    p.add_argument("--repo")
    f = sub.add_parser("flow", help="opened vs closed per filer over the window (reads only)")
    f.add_argument("--repo")
    f.add_argument("--hours", type=float, default=24.0)
    f.add_argument("--line", action="store_true", help="one plain line instead of JSON")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prune":
            print(json.dumps(read_prune(a.repo), indent=1))
        else:
            res = read_flow(a.repo, a.hours)
            print(flow_line(res) if a.line else json.dumps(res, indent=1))
    except (RuntimeError, ValueError) as exc:
        print(f"issue_flow: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
