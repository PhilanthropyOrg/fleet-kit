#!/usr/bin/env python3
"""issues_per_hour -- the fleet's headline metric: issues resolved per hour.

Reif's definition, verbatim: an issue counts only when a MERGED PR closes it (Closes #N) AND
the change is DEPLOYED. closed-as-dupe/stale/superseded counts 0 -- GitHub's own "closed" state
covers all four and does not distinguish them, so this module only ever trusts a PR's own
`closingIssuesReferences` (GitHub's own resolution of Closes/Fixes/Resolves, the same field
scoreboard.py's closed_per_run() already reads), never a bare "closed" state.

A mega issue (marie, scripts/issue_cluster.py) closed this way counts its folded child issues --
"3 folded into 1 and delivered is worth 3," Reif's own worked example, taken literally: no bonus
for the mega's own issue on top of its children. A mega closed with NO child issues at all
(nothing folded into it yet) is worth 1, same as any other single issue.

INPUTS, kept as plain data so this stays pure and testable -- same shapes fleet_view_server.py's
scoreboard block already fetches via gh, cached (`_scoreboard_merged_prs`, `_scoreboard_deploy_runs`
in fleet_view_server.py):
  - merged_prs: [{"number", "mergedAt" (ISO8601), "mergeCommit": {"oid": sha},
    "closingIssuesReferences": [{"number": int}, ...]}] -- one extra field
    (`mergeCommit.oid`) added to `_scoreboard_merged_prs`'s existing `--json` list for this
    metric; everything else is already fetched today.
  - deploy_runs: [{"createdAt", "conclusion", "headSha"}] -- exactly `_scoreboard_deploy_runs`'s
    existing shape, no changes needed.
  - megas: {mega_issue_number: [child_issue_number, ...]} -- from issue_cluster.py's own mega
    body checklist (`- [ ] #N ...` lines under "## Children"), parsed by whoever assembles the
    input (fleet_view_server.py, at query time, from `gh issue view --json body`); this module
    takes the already-parsed map so it never has to know about gh or issue bodies itself.
  - is_ancestor(sha, deployed_sha) -> bool: whether `sha` shipped as of the deploy that landed
    `deployed_sha` -- callers pass a real git ancestor-check (git_is_ancestor() below) so tests
    can exercise the credit logic with a fake and no real git repo.

CREDIT, not deploy TIME, is what "resolved" means for the hourly bucket: an issue is credited
into the hour its PR MERGED, not the hour it happened to deploy, since deploys batch multiple
merges into one cutover and the fleet's own pace is measured by how fast it finishes work, not
by the deploy driver's batching cadence. A merge with no deploy yet by query time simply isn't
credited yet -- it moves into the right bucket retroactively once deployed_by() reports it as
deployed (the caller re-runs this over the same window; nothing here is stateful).
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def hour_bucket(epoch: float) -> str:
    """UTC hour bucket key, e.g. '2026-09-24T13:00Z' -- one string per hour, sortable as text."""
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:00Z")


def _merge_sha(pr: dict) -> str:
    return ((pr.get("mergeCommit") or {}).get("oid")) or pr.get("merge_commit_sha") or ""


def _closes(pr: dict) -> list[int]:
    return [c["number"] for c in (pr.get("closingIssuesReferences") or []) if c.get("number") is not None]


def credit_for_pr(pr: dict, megas: dict[int, list[int]]) -> tuple[int, int]:
    """(direct_credit, mega_child_credit) this one merged PR is worth, BEFORE any deploy check
    -- deploy-gating happens once, over the whole PR, in resolved_issues() below (a PR either
    deployed or it didn't; there's no per-issue partial-deploy state).

    direct: each closed issue number that is a standalone issue -- not a mega's own number,
    and not itself listed as a child of some other mega (a child is normally closed by marie's
    separate "tracked in #M" close, not a build PR -- but if a build PR names one directly this
    still credits it once as a mega-child, the more specific fact, never twice).
    mega_child: a closed mega's own folded-child count -- see module docstring for why there is
    no bonus beyond that."""
    child_to_mega = {c: m for m, kids in megas.items() for c in kids}
    direct = 0
    mega_child = 0
    for n in _closes(pr):
        if n in megas:
            mega_child += len(megas[n]) or 1
        elif n in child_to_mega:
            continue  # counted via its mega's len() above regardless of scan order
        else:
            direct += 1
    return direct, mega_child


def deployed_by(merge_sha: str | None, deploy_runs: list[dict], is_ancestor) -> float | None:
    """Epoch ts of the first successful deploy run that shipped `merge_sha`, or None if no
    deploy has (yet). `is_ancestor(sha, deploy_sha)` -> bool is the caller's git check (real git
    in production, a fake in tests) so this stays a pure search over already-fetched deploy
    records. Only `conclusion in (None, "", "success")` runs count -- same filter
    scoreboard.live_merges() already applies to the identical `deploy_runs` shape. Runs are
    scanned oldest-first so "deployed_by" is always the FIRST cutover that shipped it, not the
    most recent."""
    if not merge_sha:
        return None
    candidates = [d for d in deploy_runs if d.get("conclusion") in (None, "", "success")]
    for d in sorted(candidates, key=lambda d: _parse_iso(d.get("createdAt")) or 0):
        if is_ancestor(merge_sha, d.get("headSha") or ""):
            return _parse_iso(d.get("createdAt"))
    return None


def resolved_issues(merged_prs: list[dict], deploy_runs: list[dict], megas: dict[int, list[int]],
                     is_ancestor) -> list[dict]:
    """One record per merged-and-deployed PR that closed something: {"pr", "merged_at",
    "deployed_at", "hour" (merge hour bucket), "direct", "mega_child"}. A PR that closed
    nothing, or hasn't deployed yet, or was a real dupe/stale close (never reached via
    `closingIssuesReferences` at all -- that field is GitHub's own resolution, and a dupe close
    is never a Closes-ref merge) is simply absent, i.e. worth 0."""
    out = []
    for pr in merged_prs:
        closes = _closes(pr)
        if not closes:
            continue
        merged_at = _parse_iso(pr.get("mergedAt"))
        if merged_at is None:
            continue
        dep_ts = deployed_by(_merge_sha(pr), deploy_runs, is_ancestor)
        if dep_ts is None:
            continue  # merged but undeployed: 0, not yet -- re-run later once it ships
        direct, mega_child = credit_for_pr(pr, megas)
        if direct == 0 and mega_child == 0:
            continue
        out.append({
            "pr": pr.get("number"), "merged_at": pr.get("mergedAt"), "deployed_at": dep_ts,
            "hour": hour_bucket(merged_at), "direct": direct, "mega_child": mega_child,
        })
    return out


def closed_without_pr(closed_issues: list[dict], merged_prs: list[dict]) -> list[dict]:
    """Issues GitHub shows closed in the window that no counted merged PR closed -- dupe/
    stale/superseded closes, and mega-children closed by marie's own "tracked in #M" close
    (real, tracked work, but not itself a merged-PR close). Separate, muted series: real fleet
    activity, explicitly not credited toward the headline rate. `closed_issues` items need
    "number" and "closedAt" (ISO8601, gh's own field name)."""
    credited = {n for pr in merged_prs for n in _closes(pr)}
    out = []
    for it in closed_issues:
        n = it.get("number")
        if n in credited:
            continue
        closed_at = _parse_iso(it.get("closedAt"))
        if closed_at is None:
            continue
        out.append({"issue": n, "closed_at": it.get("closedAt"), "hour": hour_bucket(closed_at)})
    return out


def hourly_series(resolved: list[dict], closed_no_pr: list[dict], hours: int = 24 * 7,
                   now: float | None = None) -> dict:
    """Bucket everything into the last `hours` UTC hours (default 7 days), stacked
    direct/mega_child/closed_without_pr, plus rolling 24h and 7d averages of the credited
    total (direct + mega_child) per hour -- the two lines the graph draws over the bars."""
    now = now if now is not None else datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
    end_hour = int(now // 3600) * 3600
    buckets = [end_hour - i * 3600 for i in range(hours - 1, -1, -1)]
    keys = [hour_bucket(b) for b in buckets]
    direct = {k: 0 for k in keys}
    mega_child = {k: 0 for k in keys}
    no_pr = {k: 0 for k in keys}
    for r in resolved:
        if r["hour"] in direct:
            direct[r["hour"]] += r["direct"]
            mega_child[r["hour"]] += r["mega_child"]
    for c in closed_no_pr:
        if c["hour"] in no_pr:
            no_pr[c["hour"]] += 1
    totals = [direct[k] + mega_child[k] for k in keys]

    def avg_over(n_hours: int, idx: int) -> float:
        lo = max(0, idx - n_hours + 1)
        window = totals[lo:idx + 1]
        return round(sum(window) / len(window), 3) if window else 0.0

    return {
        "hours": keys,
        "direct": [direct[k] for k in keys],
        "mega_child": [mega_child[k] for k in keys],
        "closed_without_pr": [no_pr[k] for k in keys],
        "avg_24h": [avg_over(24, i) for i in range(len(keys))],
        "avg_7d": [avg_over(24 * 7, i) for i in range(len(keys))],
        "current_24h_rate": avg_over(24, len(keys) - 1),
    }


def chart_svg(series: dict, w: int = 1200, h: int = 160) -> str:
    """The full-width stacked-bar chart as a ready-to-inject SVG string -- built here, not in
    fleet_home.html's own JS, so the page's hard byte cap (selftest.py: `len(page.encode()) <
    48_000`, fk#645 "simplify down, way down") is never spent on chart-rendering code. Colors
    are CSS custom properties (var(--accent)/var(--ok)/var(--faint)/var(--ink-2)) so the SVG
    still follows the page's own light/dark theme even though it's generated server-side."""
    hours = series["hours"]
    n = len(hours)
    if not n:
        return ""
    pad, pb = 5, 4
    ph = h - pad - pb
    bw = (w - 2 * pad) / n
    hi = max([1] + [series["direct"][i] + series["mega_child"][i] + series["closed_without_pr"][i]
                    for i in range(n)] + series["avg_24h"] + series["avg_7d"])

    def sy(v: float) -> float:
        return pad + ph - v / hi * ph

    def sx(i: int) -> float:
        return pad + i * bw

    bars = []
    for i in range(n):
        top = pad + ph
        for color, v in (("var(--faint)", series["closed_without_pr"][i]),
                         ("var(--ok)", series["mega_child"][i]),
                         ("var(--accent)", series["direct"][i])):
            if not v:
                continue
            bh = v / hi * ph
            top -= bh
            bars.append(f'<rect fill="{color}" x="{sx(i) + bw * .08:.1f}" y="{top:.1f}" '
                        f'width="{bw * .84:.1f}" height="{bh:.1f}"/>')

    def points(arr: list[float]) -> str:
        return " ".join(f"{sx(i) + bw / 2:.1f},{sy(v):.1f}" for i, v in enumerate(arr))

    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display:block;width:100%;height:{h}px">'
            + "".join(bars)
            + f'<polyline fill="none" stroke="var(--faint)" stroke-dasharray="3 3" points="{points(series["avg_7d"])}"/>'
            + f'<polyline fill="none" stroke="var(--ink-2)" points="{points(series["avg_24h"])}"/>'
            + "</svg>")


# ---------------------------------------------------------------- production data sources


_SIG_MARKER = "mega-signature:"


def load_megas(repo: str | None, run=None) -> dict[int, list[int]]:
    """{mega_number: [child_number, ...]} read from every open+closed `fleet:mega` issue's own
    checklist body (scripts/issue_cluster.py's `- [ ] #N ...` lines under "## Children") --
    real gh calls; production-only, never exercised by the pure tests above."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=90))
    repo_args = ["--repo", repo] if repo else []
    megas: dict[int, list[int]] = {}
    for state in ("open", "closed"):
        r = run(["gh", "issue", "list", *repo_args, "--state", state, "--label", "fleet:mega",
                 "--limit", "500", "--json", "number,body"])
        if r.returncode != 0:
            continue
        for issue in json.loads(r.stdout or "[]"):
            body = issue.get("body") or ""
            if _SIG_MARKER not in body:
                continue
            children = [int(n) for n in re.findall(r"^- \[[ x]\] #(\d+)", body, re.M)]
            megas[issue["number"]] = children
    return megas


def git_is_ancestor(candidate_sha: str, deployed_sha: str, repo_dir: str | Path = ".") -> bool:
    """True if `candidate_sha` is `deployed_sha` itself or one of its ancestors -- i.e. shipped
    by the deploy that landed `deployed_sha`. Missing/unknown shas (a candidate git doesn't
    have, e.g. a shallow clone) fail closed (False, not deployed) rather than raise."""
    if not candidate_sha or not deployed_sha:
        return False
    r = subprocess.run(
        ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", candidate_sha, deployed_sha],
        capture_output=True, text=True, timeout=15,
    )
    return r.returncode == 0
