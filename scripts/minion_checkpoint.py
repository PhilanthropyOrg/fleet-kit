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
  * ended        -- same as timed-out, after the pass exited rc=0 with no Outcome: line
                    (2026-09-27: 56 minions ended "waiting for the background run"). Skipped
                    when the branch already has an open non-draft PR.
The first two run while the model is still working, so they never touch the index; only the
last one (the model is dead by then) commits.

A CHECKPOINT PR NEVER MERGES AS A CHECKPOINT (2026-09-26 08:32 UTC, #8110). The green save
opened #8110 as a draft; two minutes later the same pass hit "Pull request is a draft" arming
auto-merge, ran `gh pr ready` (minion.md step 7 said to), re-armed, and the partial slice of
#7942 merged 12s later with its "WIP" title and "Part of #7942" body intact. The rule, in code:
  * `ready` is the ONLY way a checkpoint PR leaves draft. It runs from the pass whose worktree
    is on the PR's branch, and only when the title is no longer WIP, the body is no longer the
    auto-written text, every item is `Closes #N` or `Part of #N` + `Remaining:` (a green slice
    ships; fk#1481), and the pushed HEAD has a passing verified_test receipt. Otherwise it prints the gaps and leaves the PR a draft.
  * merge_arm.sh and judge-judy refuse to arm auto-merge on a checkpoint PR, and
    checkpoint_pr_hook.py blocks a raw `gh pr ready` / `gh pr merge` on one.
`ready` strips MARKER on its way out, so a finished PR is an ordinary PR again.

HQ FINISHES THE GREEN ONES (philanthropy#8218 step 4). A minion that finished its items but
died before `ready` leaves a draft nobody readies. `complete` lists open minion drafts whose
title/body pass done_gaps and whose CI is all green on the head; `ready --pr N --ci` readies one
from anywhere (HQ on the host has no worktree on the branch), taking green CI on the PR's head in
place of the local receipt. Same done-criteria, so #8110 still stays a draft. `complete
--notify` also messages `hq` (kind merge-ready) on the fleet_msg bus.

RESUME. `find` returns the newest pushed minion branch whose items are all in this pass's
items, so run_member.sh can build the worktree ON that branch (same name, same draft PR)
instead of fresh off main. A branch carrying an item this pass was NOT handed is never picked:
that would drag someone else's half-built work into this PR.

Usage:
  minion_checkpoint.py save --wt DIR --branch B --items 1,2 --reason green|pre-timeout|timed-out
  minion_checkpoint.py find --items 1,2 [--repo DIR]      # prints a branch name, or nothing
  minion_checkpoint.py watch --wt DIR --branch B --items 1,2 --deadline EPOCH --pid PID
  minion_checkpoint.py ready [--wt DIR] [--pr N]           # the only way out of draft
  minion_checkpoint.py ready --pr N --ci                    # no worktree: green CI on the head
  minion_checkpoint.py complete [--repo DIR] [--notify]     # drafts `ready --ci` would accept
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
import pr_ci_wait  # noqa: E402 -- one definition of "the newest attempt of each check"

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


def resumable_from(prs: list[dict]) -> list[dict]:
    """Open DRAFT PRs on a minion branch -> [{"pr", "branch", "items"}]. Pure.

    Checkpoint drafts, and drafts a minion opened for part-done work, both mean "a minion's
    unfinished work, waiting for the next minion", never "someone else owns this item". On
    2026-09-26 gru dropped #7939 and #7950 as "owned by open draft PRs #8135/#8133", so neither
    was resumed. gru.md step 2a reads this list."""
    out = []
    for p in prs:
        m = BRANCH_RE.match(p.get("headRefName") or "")
        if m and p.get("isDraft"):
            out.append({"pr": int(p["number"]), "branch": p["headRefName"],
                        "items": [int(n) for n in m.group(1).split("_")]})
    return sorted(out, key=lambda r: r["pr"])


def resumable(repo: str) -> list[dict] | None:
    rc, out = _gh(["pr", "list", "--state", "open", "--draft", "--limit", "200",
                   "--json", "number,isDraft,headRefName"], cwd=repo, timeout=120)
    if rc != 0:
        return None
    try:
        return resumable_from(json.loads(out))
    except json.JSONDecodeError:
        return None


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


def open_pr_is_ready(wt: str, branch: str) -> bool:
    rc, out = _gh(["pr", "list", "--head", branch, "--state", "open", "--json", "isDraft",
                   "--jq", ".[0].isDraft // empty"], cwd=wt)
    return rc == 0 and out.strip() == "false"


def save(wt: str, branch: str, items: list[int], reason: str) -> dict:
    res: dict = {"reason": reason, "branch": branch, "items": items, "saved": False}
    # `ended`: the pass exited on its own with no report (run_member.sh, rc=0). A non-draft PR
    # on the branch may have auto-merge armed -- untested WIP must never land on that.
    if reason == "ended" and open_pr_is_ready(wt, branch):
        res["why"] = "open non-draft PR on this branch -- not pushing untested WIP under auto-merge"
        return res
    if reason in ("timed-out", "killed", "ended"):
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
    """What still stands between this PR and a shippable slice. Pure, so the rule is testable.
    A slice ships: `Part of #N` + a `Remaining:` line is fine (minion.md step 7). Requiring
    every item closed kept epics in draft forever: 1 of 47 checkpoints merged 09-23..09-30,
    37 were closed unmerged, #7661 alone chained five (fk#1481)."""
    gaps = []
    if not items:
        gaps.append("no items: not a minion checkpoint branch")
    if re.match(r"\s*\[?wip\b", title or "", re.I):
        gaps.append("title still says WIP: `gh pr edit --title` with what the PR does")
    if "**Draft checkpoint**" in (body or ""):
        gaps.append("body is still the auto-written checkpoint text: say what this PR ships")
    closed = {int(n) for n in CLOSES_RE.findall(body or "")}
    part = {int(n) for n in re.findall(r"(?i)\bpart of #(\d+)", body or "")}
    for n in items:
        if n not in closed | part:
            gaps.append(f"#{n} is neither `Closes #{n}` nor `Part of #{n}` in the body")
    if part and not re.search(r"(?im)^\W*remaining\s*:", body or ""):
        gaps.append("`Part of #N` with no `Remaining:` line: say what is left")
    return gaps


def ci_green(rollup: list[dict] | None) -> bool:
    """Every check's newest attempt finished OK, and there is at least one. Pure."""
    checks = pr_ci_wait.latest_checks(rollup or [])
    return bool(checks) and all(
        (c.get("conclusion") or c.get("state") or "").upper() in pr_ci_wait.DONE_OK
        for c in checks.values())


def ready(wt: str, pr: int | None = None, ci: bool = False) -> dict:
    """Mark this pass's checkpoint PR ready, only if its done-criteria are met. With `ci`
    (HQ, no worktree on the branch): green CI on the PR's head stands in for the receipt."""
    if ci and not pr:
        return {"ready": False, "branch": "", "gaps": ["--ci needs --pr N"]}
    rc, branch = (0, "") if ci else _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    res: dict = {"ready": False, "branch": branch if rc == 0 else ""}
    rc, out = _gh(["pr", "view", str(pr) if pr else res["branch"], "--json",
                   "number,title,body,headRefName,headRefOid,isDraft,state"
                   + (",statusCheckRollup" if ci else "")], cwd=wt)
    try:
        v = json.loads(out) if rc == 0 else None
    except ValueError:
        v = None
    if not v:
        res["gaps"] = [f"could not read the PR: {(out or '')[-200:]}"]
        return res
    res["pr"] = v["number"]
    if ci:
        res["branch"] = v.get("headRefName") or ""
    elif v.get("headRefName") != res["branch"]:
        res["gaps"] = [f"PR #{v['number']} is on {v.get('headRefName')!r}, this worktree is on "
                       f"{res['branch']!r}: only the pass building that branch may mark it ready"]
        return res
    gaps = done_gaps(v.get("title") or "", v.get("body") or "", branch_items(res["branch"]))
    if ci:
        if not ci_green(v.get("statusCheckRollup")):
            gaps.append("CI on the PR's head is not all green: wait for it")
    else:
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


# --- complete: green drafts HQ can finish (philanthropy#8218) -----------------------------

COMPLETE_FIELDS = "number,title,body,isDraft,headRefName,headRefOid,statusCheckRollup"


def complete_from(prs: list[dict]) -> list[dict]:
    """Open minion DRAFT PRs that are done (done_gaps empty) and green on their head. Pure."""
    out = []
    for p in prs:
        items = branch_items(p.get("headRefName") or "")
        if not (p.get("isDraft") and items):
            continue
        if done_gaps(p.get("title") or "", p.get("body") or "", items):
            continue
        if not ci_green(p.get("statusCheckRollup")):
            continue
        out.append({"pr": int(p["number"]), "branch": p["headRefName"], "items": items,
                    "head": p.get("headRefOid"), "title": p.get("title") or ""})
    return sorted(out, key=lambda r: r["pr"])


def complete(repo: str) -> list[dict] | None:
    rc, out = _gh(["pr", "list", "--state", "open", "--draft", "--limit", "200",
                   "--json", COMPLETE_FIELDS], cwd=repo, timeout=120)
    if rc != 0:
        return None
    try:
        return complete_from(json.loads(out))
    except json.JSONDecodeError:
        return None


def merge_ready_message(done: list[dict]) -> dict | None:
    """ONE bus message to hq naming the drafts to finish. Pure; keyed on the PR set."""
    if not done:
        return None
    lines = [f"{len(done)} minion draft(s) are done and green. For each: "
             "`minion_checkpoint.py ready --pr N --ci`, then `gh pr merge N --auto --squash "
             "--match-head-commit <head>`."]
    lines += [f"- PR #{d['pr']} ({d['head'][:10] if d.get('head') else '?'}): {d['title']}"
              for d in done]
    return {"to": ["hq"], "kind": "merge-ready",
            "key": "merge-ready:" + ",".join(str(d["pr"]) for d in done),
            "body": "\n".join(lines), "items": [d["pr"] for d in done]}


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
    s.add_argument("--reason", required=True, choices=["green", "pre-timeout", "timed-out", "killed", "ended"])
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
    rs = sub.add_parser("resumable", help="open minion draft PRs whose items the next minion resumes")
    rs.add_argument("--repo", default=".")
    r = sub.add_parser("ready")
    r.add_argument("--wt", default=os.environ.get("WT_PATH") or ".")
    r.add_argument("--pr", type=int)
    r.add_argument("--ci", action="store_true",
                   help="no worktree on the branch (HQ): green CI on the PR head replaces the receipt")
    cp = sub.add_parser("complete", help="read-only: done + green minion drafts, as JSON")
    cp.add_argument("--repo", default=".")
    cp.add_argument("--notify", action="store_true", help="also message hq (kind merge-ready)")
    a = ap.parse_args(argv)
    if a.cmd == "complete":
        res = complete(a.repo)
        if res is None:
            print("error: gh pr list failed", file=sys.stderr)
            return 1
        m = merge_ready_message(res) if a.notify else None
        if m:
            try:
                import fleet_db
                import fleet_msg
                fleet_msg.send(fleet_db.connect(), "gru", m["to"], m["kind"], m["key"], m["body"],
                               m["items"])
            except Exception as exc:  # noqa: BLE001 -- the list itself must still print
                print(f"minion_checkpoint: merge-ready message not sent: {exc}", file=sys.stderr)
        print(json.dumps(res))
        return 0
    if a.cmd == "ready":
        res = ready(a.wt, a.pr, ci=a.ci)
        print(json.dumps(res))
        if not res["ready"]:
            print("NOT READY -- the PR stays a draft:\n" + "\n".join(f"  - {g}" for g in res["gaps"]),
                  file=sys.stderr)
        return 0 if res["ready"] else 3
    if a.cmd == "resumable":
        res = resumable(a.repo)
        if res is None:
            print("error: gh pr list failed", file=sys.stderr)
            return 1
        print(json.dumps(res))
        return 0
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
