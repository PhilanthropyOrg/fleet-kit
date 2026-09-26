#!/usr/bin/env python3
"""minion_checkpoint.py -- a minion pass that dies at its timeout must leave its work behind.

THE GAP (2026-09-25 09:19 and 2026-09-26 02:37 UTC). A minion batch (#7938 #7939 #7941 #7950)
ran its full 5400s, hit rc=124 and left nothing: no pushed branch, no PR. run_member.sh then
removed the worktree, gru released the claims, and the next pass started from zero. ~90 minutes
of paid work gone, twice.

THREE SAVES, all pushing the pass's own branch and opening (once) a DRAFT PR:
  * green        -- the first commit whose tree has a passing verified_test.sh receipt.
  * pre-timeout  -- FLEET_CHECKPOINT_LEAD_S before the timeout: push whatever commits exist.
  * timed-out    -- after rc=124, before the worktree is removed: also commit the uncommitted
                    work as a WIP commit, so nothing that was typed is lost.
  * killed       -- same as timed-out, from run_member.sh's SIGTERM trap. A deploy retires the
                    old container and SIGTERMs every pass still running 15 min later (60s grace);
                    on 2026-09-26 06:42 that killed #7938 #7939 #7941 #7950 34 min in, unsaved.
                    The push comes before the PR call, so even a save the grace window cuts off
                    leaves a branch the next pass resumes.
The first two run while the model is still working, so they never touch the index; only the
last one (the model is dead by then) commits.

A CHECKPOINT PR NEVER MERGES AS A CHECKPOINT (2026-09-26 08:32 UTC, #8110). The green save
opened #8110 as a draft; two minutes later the same pass hit "Pull request is a draft" arming
auto-merge, ran `gh pr ready` (minion.md step 7 said to), re-armed, and the partial slice of
#7942 merged 12s later with its "WIP" title and "Part of #7942" body intact. The rule, in code:
  * `ready` is the ONLY way a checkpoint PR leaves draft. It runs from the pass whose worktree
    is on the PR's branch, and only when every item on the branch is `Closes #N`, nothing says
    `Part of` / `Remaining:`, the title is no longer WIP, and the pushed HEAD has a passing
    verified_test receipt. Otherwise it prints the gaps and leaves the PR a draft.
  * merge_arm.sh and judge-judy refuse to arm auto-merge on a checkpoint PR, and
    checkpoint_pr_hook.py blocks a raw `gh pr ready` / `gh pr merge` on one.
`ready` strips MARKER on its way out, so a finished PR is an ordinary PR again.

RESUME. `find` returns the newest pushed minion branch whose items are all in this pass's
items, so run_member.sh can build the worktree ON that branch (same name, same draft PR)
instead of fresh off main. A branch carrying an item this pass was NOT handed is never picked:
that would drag someone else's half-built work into this PR.

Usage:
  minion_checkpoint.py save --wt DIR --branch B --items 1,2 --reason green|pre-timeout|timed-out
  minion_checkpoint.py find --items 1,2 [--repo DIR]      # prints a branch name, or nothing
  minion_checkpoint.py watch --wt DIR --branch B --items 1,2 --deadline EPOCH --pid PID
  minion_checkpoint.py ready [--wt DIR] [--pr N]           # the only way out of draft
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pretest_push_hook import receipt_path  # noqa: E402 -- one definition of the receipt

MARKER = "<!-- fleet-checkpoint -->"
TITLE_PREFIX = "WIP (minion checkpoint)"
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
BRANCH_RE = re.compile(r"^member/minion-item(\d+(?:_\d+)*)-(\d+)-(\d+)$")
WIP_MAX_BYTES = 1_000_000  # an untracked file bigger than this is a screenshot or a dump, not work


def _git(wt: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def _gh(args: list[str], cwd: str, timeout: int = 90) -> tuple[int, str]:
    try:
        p = subprocess.run([os.environ.get("FLEET_GH_BIN", "gh"), *args], capture_output=True,
                           text=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def parse_items(s: str) -> list[int]:
    return [int(x) for x in re.split(r"[,_\s]+", s or "") if x.strip().isdigit()]


# --- resume --------------------------------------------------------------------------------

def pick_resume_branch(branches: list[str], items: list[int]) -> str | None:
    """Newest minion branch (by the epoch in its name) whose item set is a non-empty subset of
    `items`. Pure, so the rule is testable without a remote."""
    want = set(items)
    best: tuple[int, str] | None = None
    for b in branches:
        m = BRANCH_RE.match(b)
        if not m:
            continue
        have = {int(n) for n in m.group(1).split("_")}
        if not have or not have <= want:
            continue
        ts = int(m.group(3))
        if best is None or ts > best[0]:
            best = (ts, b)
    return best[1] if best else None


def remote_minion_branches(repo: str) -> list[str]:
    rc, out = _git(repo, "ls-remote", "--heads", "origin", "member/minion-item*", timeout=60)
    if rc != 0:
        return []
    return [line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line]


def find(repo: str, items: list[int]) -> str | None:
    return pick_resume_branch(remote_minion_branches(repo), items)


# --- save ----------------------------------------------------------------------------------

def commits_ahead(wt: str, base: str = "origin/main") -> int:
    rc, out = _git(wt, "rev-list", "--count", f"{base}..HEAD")
    return int(out) if rc == 0 and out.isdigit() else 0


def head_is_green(wt: str) -> bool:
    """HEAD's tree is exactly what the last passing verified_test.sh run tested."""
    try:
        data = json.loads(receipt_path(wt).read_text())
    except (OSError, ValueError):
        return False
    rc, tree = _git(wt, "rev-parse", "HEAD^{tree}")
    return rc == 0 and data.get("status") == "pass" and data.get("content") == tree


def commit_wip(wt: str, items: list[int]) -> bool:
    """Stage tracked changes plus small untracked files and commit them. Only after the model is
    dead (timed-out or killed): while it runs, the index is its own."""
    _git(wt, "add", "-u")
    rc, out = _git(wt, "ls-files", "--others", "--exclude-standard", "-z")
    if rc == 0:
        for f in [x for x in out.split("\0") if x]:
            p = Path(wt) / f
            if p.is_file() and p.stat().st_size <= WIP_MAX_BYTES:
                _git(wt, "add", "--", f)
    rc, _ = _git(wt, "diff", "--cached", "--quiet")
    if rc == 0:
        return False  # nothing staged
    refs = " ".join(f"#{n}" for n in items)
    rc, _ = _git(wt, "-c", "user.name=fleet-minion", "-c", "user.email=fleet-minion@users.noreply.github.com",
                 "commit", "--no-verify", "-m",
                 f"wip(checkpoint): uncommitted work when the minion pass ended ({refs})\n\n"
                 "Saved by minion_checkpoint.py so the next pass resumes instead of restarting. "
                 "Not tested: re-run verified_test.sh before building on it.")
    return rc == 0


def pr_body(items: list[int], reason: str, branch: str) -> str:
    lines = [MARKER,
             f"**Draft checkpoint** ({reason}) of a minion pass on `{branch}`. The next pass handed "
             "these items resumes this branch and this PR; it marks the PR ready when done.", ""]
    lines += [f"Part of #{n}" for n in items]
    lines += ["", "Remaining: the minion pass ended before finishing; see its commits for what landed."]
    return "\n".join(lines)


def open_pr_for(wt: str, branch: str) -> int | None:
    rc, out = _gh(["pr", "list", "--head", branch, "--state", "open", "--json", "number",
                   "--jq", ".[0].number // empty"], cwd=wt)
    return int(out) if rc == 0 and out.strip().isdigit() else None


def save(wt: str, branch: str, items: list[int], reason: str) -> dict:
    res: dict = {"reason": reason, "branch": branch, "items": items, "saved": False}
    if reason in ("timed-out", "killed"):
        res["wip_commit"] = commit_wip(wt, items)
    res["commits"] = commits_ahead(wt)
    if res["commits"] == 0:
        res["why"] = "no commits ahead of origin/main"
        return res
    if reason == "green" and not head_is_green(wt):
        res["why"] = "HEAD has no passing test receipt yet"
        return res
    rc, out = _git(wt, "push", "--no-verify", "-u", "origin", f"HEAD:refs/heads/{branch}", timeout=180)
    if rc != 0:
        res["why"] = f"push failed: {out[-300:]}"
        return res
    res["pushed"] = True
    pr = open_pr_for(wt, branch)
    if pr is None:
        refs = ", ".join(f"#{n}" for n in items)
        rc, out = _gh(["pr", "create", "--draft", "--head", branch,
                       "--title", f"{TITLE_PREFIX}: {refs}",
                       "--body", pr_body(items, reason, branch)], cwd=wt, timeout=120)
        m = re.search(r"/pull/(\d+)", out or "")
        pr = int(m.group(1)) if rc == 0 and m else None
        res["pr_created"] = pr is not None
        if pr is None:
            res["why"] = f"push ok, draft PR not created: {(out or '')[-300:]}"
    res["pr"] = pr
    res["saved"] = True
    return res


# --- ready: the only way out of draft ------------------------------------------------------

def is_checkpoint_pr(title: str | None, body: str | None) -> bool:
    return MARKER in (body or "") or (title or "").startswith(TITLE_PREFIX)


def branch_items(branch: str) -> list[int]:
    m = BRANCH_RE.match(branch or "")
    return [int(n) for n in m.group(1).split("_")] if m else []


def done_gaps(title: str, body: str, items: list[int]) -> list[str]:
    """What still stands between this PR and "done". Pure, so the rule is testable."""
    gaps = []
    if not items:
        gaps.append("no items: not a minion checkpoint branch")
    if re.match(r"\s*\[?wip\b", title or "", re.I):
        gaps.append("title still says WIP: `gh pr edit --title` with what the PR does")
    closed = {int(n) for n in CLOSES_RE.findall(body or "")}
    for n in items:
        if n not in closed:
            gaps.append(f"#{n} is not `Closes #{n}` in the body: finish it (its done-criteria "
                        "are in the issue) or leave the PR a draft for the next pass")
    if re.search(r"(?i)\bpart of #\d+", body or ""):
        gaps.append("body still says `Part of #N`: unfinished work stays in draft")
    if re.search(r"(?im)^\W*remaining\s*:", body or ""):
        gaps.append("body still has a `Remaining:` line: unfinished work stays in draft")
    return gaps


def ready(wt: str, pr: int | None = None) -> dict:
    """Mark this pass's checkpoint PR ready, only if its done-criteria are met."""
    rc, branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    res: dict = {"ready": False, "branch": branch if rc == 0 else ""}
    rc, out = _gh(["pr", "view", str(pr) if pr else res["branch"], "--json",
                   "number,title,body,headRefName,headRefOid,isDraft,state"], cwd=wt)
    try:
        v = json.loads(out) if rc == 0 else None
    except ValueError:
        v = None
    if not v:
        res["gaps"] = [f"could not read the PR: {(out or '')[-200:]}"]
        return res
    res["pr"] = v["number"]
    if v.get("headRefName") != res["branch"]:
        res["gaps"] = [f"PR #{v['number']} is on {v.get('headRefName')!r}, this worktree is on "
                       f"{res['branch']!r}: only the pass building that branch may mark it ready"]
        return res
    gaps = done_gaps(v.get("title") or "", v.get("body") or "", branch_items(res["branch"]))
    rc, head = _git(wt, "rev-parse", "HEAD")
    if rc != 0 or head != v.get("headRefOid"):
        gaps.append("local HEAD is not the PR's head: push first")
    elif not head_is_green(wt):
        gaps.append("HEAD has no passing verified_test.sh receipt: run it")
    if gaps:
        res["gaps"] = gaps
        return res
    body = (v.get("body") or "").replace(MARKER, "").lstrip()
    rc, out = _gh(["pr", "edit", str(v["number"]), "--body", body], cwd=wt)
    if rc != 0:
        res["gaps"] = [f"could not drop the checkpoint marker: {out[-200:]}"]
        return res
    if v.get("isDraft"):
        rc, out = _gh(["pr", "ready", str(v["number"])], cwd=wt)
        if rc != 0:
            res["gaps"] = [f"gh pr ready failed: {out[-200:]}"]
            return res
    res["ready"] = True
    return res


# --- watch ---------------------------------------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch(wt: str, branch: str, items: list[int], deadline: float, pid: int,
          lead_s: int, poll_s: int, log=print) -> None:
    """Runs beside the model: save once on the first green commit, once `lead_s` before the
    deadline. Exits when `pid` (the pass) exits."""
    green_done = pre_done = False
    while _alive(pid) and not (green_done and pre_done):
        now = time.time()
        if not green_done and commits_ahead(wt) and head_is_green(wt):
            r = save(wt, branch, items, "green")
            log(json.dumps(r))
            green_done = r["saved"]
        if not pre_done and now >= deadline - lead_s:
            r = save(wt, branch, items, "pre-timeout")
            log(json.dumps(r))
            pre_done = True
        time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save")
    s.add_argument("--wt", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--items", required=True)
    s.add_argument("--reason", required=True, choices=["green", "pre-timeout", "timed-out", "killed"])
    f = sub.add_parser("find")
    f.add_argument("--items", required=True)
    f.add_argument("--repo", default=".")
    w = sub.add_parser("watch")
    w.add_argument("--wt", required=True)
    w.add_argument("--branch", required=True)
    w.add_argument("--items", required=True)
    w.add_argument("--deadline", type=float, required=True)
    w.add_argument("--pid", type=int, required=True)
    w.add_argument("--lead-s", type=int, default=int(os.environ.get("FLEET_CHECKPOINT_LEAD_S") or 600))
    w.add_argument("--poll-s", type=int, default=int(os.environ.get("FLEET_CHECKPOINT_POLL_S") or 60))
    r = sub.add_parser("ready")
    r.add_argument("--wt", default=os.environ.get("WT_PATH") or ".")
    r.add_argument("--pr", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "ready":
        res = ready(a.wt, a.pr)
        print(json.dumps(res))
        if not res["ready"]:
            print("NOT READY -- the PR stays a draft:\n" + "\n".join(f"  - {g}" for g in res["gaps"]),
                  file=sys.stderr)
        return 0 if res["ready"] else 3
    items = parse_items(a.items)
    if a.cmd == "find":
        b = find(a.repo, items)
        if b:
            print(b)
        return 0
    if a.cmd == "save":
        print(json.dumps(save(a.wt, a.branch, items, a.reason)))
        return 0
    watch(a.wt, a.branch, items, a.deadline, a.pid, a.lead_s, a.poll_s,
          log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
