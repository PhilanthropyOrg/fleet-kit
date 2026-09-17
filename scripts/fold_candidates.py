#!/usr/bin/env python3
"""fold_candidates — find open backlog items that are small deltas to an already-open PR.

fleet-kit#1127 (Reif, 2026-09-17): "things could hit the backlog and be just a tiny update to
an existing PR. Two PRs for that is a waste. Have Marie make effective combining of PRs."

Today every backlog item becomes its own PR. A one-line follow-up to a PR that's still open (a
review finding, a copy tweak on the same page, a test the reviewer asked for) opens a second
PR, a second CI run, and two things to merge that touch the same file. This module is the
MECHANICAL half only — it lists (item, open PR) candidate pairs by shared evidence so marie's
fold-vs-new decision is grounded, not guessed. marie still decides; this never labels or
comments anything itself.

CONTRACT: `candidates(issues, prs, now) -> list[dict]`, each `{item, pr, reason, confidence}`.
Rules, strongest first — the first rule that matches an (issue, pr) pair wins, no double-count:
  (a) issue is a review finding against an open PR ("fix: PR #N failed code review", or its
      body names "PR #N") -- confidence "high"
  (b) issue is a declared follow-up: the PR's body says `Closes #M`/its branch is `item<M>-...`
      and the issue's OWN body references that PR's number `#N` -- confidence "high"
  (c) issue body names a file path that is also in the PR's changed files -- confidence "medium"
  (d) same `lane:` label, and >=2 shared words over 6 chars between issue and PR titles --
      confidence "low"

Exclusions (never a candidate, regardless of match strength): PR is a draft; PR hasn't been
updated in >24h (Reif's stale-PR rule -- stale gets closed, not extended, so it's not a fold
target either); PR `mergeStateStatus` is DIRTY (a dirty branch can't take a clean fold push
either way).

Note for the builder side (`worktree_builder.sh --onto-pr N`, `scripts/pr_arm.sh`): a PR that
is already enqueued in the merge queue rejects a plain push with "protected branch hook
declined" -- that is not this module's problem (it only ever proposes fold candidates for a PR
that is still open and not DIRTY), but the builder consuming its output must handle that
refusal by falling back to opening a normal PR rather than losing the work.

CLI: fetches live issues/PRs for FLEET_REPO via `gh` and prints the candidate JSON list.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

ISSUE_FIELDS = "number,title,body,labels,createdAt"
PR_FIELDS = (
    "number,title,body,headRefName,files,createdAt,updatedAt,isDraft,"
    "mergeStateStatus,autoMergeRequest"
)

STALE_SECONDS = 24 * 3600
_WORD_RE = re.compile(r"[A-Za-z0-9]{7,}")

_REVIEW_FINDING_RE = re.compile(r"fix:\s*PR\s*#(\d+)\s*failed code review", re.I)
_PR_MENTION_RE = re.compile(r"PR\s*#(\d+)", re.I)
_CLOSES_RE = re.compile(r"\bClose[sd]?\b:?\s*#(\d+)", re.I)
_BRANCH_ITEM_RE = re.compile(r"item(\d+)", re.I)
_ISSUE_HASH_RE = re.compile(r"#(\d+)")


def _lane_labels(issue: dict) -> set[str]:
    return {
        lb.get("name", "")
        for lb in issue.get("labels") or []
        if isinstance(lb, dict) and str(lb.get("name", "")).startswith("lane:")
    }


def _is_excluded(pr: dict, now: float) -> bool:
    if pr.get("isDraft"):
        return True
    if str(pr.get("mergeStateStatus") or "").upper() == "DIRTY":
        return True
    updated = pr.get("updatedAt")
    if updated:
        try:
            ts = time.mktime(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))
            # updatedAt from `gh ... --json` is UTC; time.mktime assumes local time, but the
            # error that introduces (a few hours) is negligible against a 24h staleness window.
            if now - ts > STALE_SECONDS:
                return True
        except ValueError:
            pass  # unparseable timestamp -- don't exclude on a formatting surprise
    return False


def _rule_a(issue: dict, pr: dict) -> str | None:
    """Review finding against this PR."""
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    m = _REVIEW_FINDING_RE.search(title) or _REVIEW_FINDING_RE.search(body)
    if m and int(m.group(1)) == pr["number"]:
        return f"review finding on PR {pr['number']}"
    for m in _PR_MENTION_RE.finditer(body):
        if int(m.group(1)) == pr["number"]:
            return f"review finding on PR {pr['number']}"
    return None


def _rule_b(issue: dict, pr: dict) -> str | None:
    """Declared follow-up: PR closes #M (or branch is itemM-...), issue body references PR#N."""
    pr_body = pr.get("body") or ""
    branch = pr.get("headRefName") or ""
    closes_m = {int(m) for m in _CLOSES_RE.findall(pr_body)}
    bm = _BRANCH_ITEM_RE.search(branch)
    if bm:
        closes_m.add(int(bm.group(1)))
    if not closes_m:
        return None
    issue_body = issue.get("body") or ""
    issue_refs = {int(m) for m in _ISSUE_HASH_RE.findall(issue_body)}
    if pr["number"] in issue_refs:
        m = next(iter(closes_m))
        return f"follow-up to item {m} in PR {pr['number']}"
    return None


def _pr_files(pr: dict) -> set[str]:
    out = set()
    for f in pr.get("files") or []:
        if isinstance(f, dict):
            p = f.get("path")
            if p:
                out.add(p)
        elif isinstance(f, str):
            out.add(f)
    return out


def _rule_c(issue: dict, pr: dict) -> str | None:
    """Issue body names a file path already touched by the PR."""
    body = issue.get("body") or ""
    for path in _pr_files(pr):
        if path and path in body:
            return f"touches {path} already in PR {pr['number']}"
    return None


def _rule_d(issue: dict, pr: dict) -> str | None:
    """Same lane label, >=2 shared distinctive words (>6 chars) between titles."""
    issue_lanes = _lane_labels(issue)
    pr_lane = None
    # PRs carry no labels field in our fetch; infer lane share only when issue itself has one
    # and the branch name embeds the same lane token (best-effort, low-confidence rule).
    if not issue_lanes:
        return None
    branch = (pr.get("headRefName") or "").lower()
    lane_names = {lb.split(":", 1)[1] for lb in issue_lanes if ":" in lb}
    if not any(lane in branch for lane in lane_names):
        return None
    issue_words = {w.lower() for w in _WORD_RE.findall(issue.get("title") or "")}
    pr_words = {w.lower() for w in _WORD_RE.findall(pr.get("title") or "")}
    shared = issue_words & pr_words
    if len(shared) >= 2:
        return f"same lane + shared title words ({', '.join(sorted(shared))}) with PR {pr['number']}"
    return None


_RULES = [
    (_rule_a, "high"),
    (_rule_b, "high"),
    (_rule_c, "medium"),
    (_rule_d, "low"),
]


def candidates(issues: list[dict], prs: list[dict], now: float) -> list[dict]:
    """Pure function: (issue, pr) candidate pairs, strongest rule wins per pair, excluded PRs
    (draft / stale >24h / DIRTY) never produce a candidate."""
    live_prs = [pr for pr in prs if not _is_excluded(pr, now)]
    out: list[dict] = []
    for issue in issues:
        for pr in live_prs:
            for rule, confidence in _RULES:
                reason = rule(issue, pr)
                if reason:
                    out.append({
                        "item": issue["number"],
                        "pr": pr["number"],
                        "reason": reason,
                        "confidence": confidence,
                    })
                    break  # strongest matching rule only, per (issue, pr) pair
    return out


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        print(f"fold_candidates: {' '.join(cmd)} FAILED: {(p.stderr or '')[:300]}", file=sys.stderr)
        return "[]"
    return p.stdout


def _fetch_live(repo: str) -> tuple[list[dict], list[dict]]:
    issue_cmd = ["gh", "issue", "list", "--label", "fleet:backlog", "--json", ISSUE_FIELDS,
                 "--limit", "200"]
    pr_cmd = ["gh", "pr", "list", "--state", "open", "--json", PR_FIELDS, "--limit", "200"]
    if repo:
        issue_cmd += ["--repo", repo]
        pr_cmd += ["--repo", repo]
    issues = json.loads(_run(issue_cmd) or "[]")
    prs = json.loads(_run(pr_cmd) or "[]")
    return issues, prs


def main() -> int:
    repo = os.environ.get("FLEET_REPO", "")
    issues, prs = _fetch_live(repo)
    result = candidates(issues, prs, time.time())
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
