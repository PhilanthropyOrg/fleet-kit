#!/usr/bin/env python3
"""pr_ci_wait.py -- wait for a PR's CI in the FOREGROUND and say exactly what is red.

THE GAP (2026-09-25, philanthropy #7975 #7982 #7986, earlier #7858 #7885 #7867): a minion pass
pushed, armed auto-merge, and ended its turn. minion.md step 9 said "arming auto-merge IS
finishing the job". CI then went red (ruff I001, the repo-health ratchet, a failing test) or
judge-judy BLOCKed the head, and nobody owned the PR any more. The builder, who had the whole
context loaded, was gone. The PRs sat red for 2-6 hours.

This is the one tool every fixer uses (minion after its push, the-fixer on an --item sub-pass,
and pr_done_hook.py's Stop check). It answers one question, "is this PR done?":

    python3 pr_ci_wait.py <pr> [--repo owner/name] [--timeout 540] [--no-wait] [--json]

  exit 0  GREEN or MERGED -- every check on the current head passed and no review BLOCK stands
  exit 1  RED or BLOCK    -- prints each failing check with the failing lines of its log, and
                             the review findings, so the caller can fix them without a second
                             round of digging
  exit 3  PENDING         -- still running when --timeout ran out; call it again
  exit 4  CLOSED          -- closed without merging
  exit 2  could not read the PR at all

The default --timeout (540s) fits inside one Bash tool call's 600s ceiling, so a caller can
block on it in the foreground. Nothing here is run in the background.

classify() is pure: red_prs.py and pr_done_hook.py import it, so "red" means the same thing to
the detector, the router and the fixer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

REVIEW_CONTEXT = os.environ.get("FLEET_REVIEW_CONTEXT", "fleet-code-review")
BLOCK_MARK = f"**{REVIEW_CONTEXT}: BLOCK**"
PASS_MARKS = (f"**{REVIEW_CONTEXT}: PASS**", f"**{REVIEW_CONTEXT}: APPROVE")

# A check that ended one of these ways on the CURRENT head blocks the PR. CANCELLED counts: a
# run cancelled on the head that is still current was not superseded by anything, so nothing
# will ever re-run it on its own.
RED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED",
                   "STALE"}
RED_STATES = {"FAILURE", "ERROR"}
DONE_OK = {"SUCCESS", "SKIPPED", "NEUTRAL"}

PR_FIELDS = ("number,state,headRefName,headRefOid,isDraft,autoMergeRequest,mergeStateStatus,"
             "statusCheckRollup,commits,comments,labels,createdAt,url")


def _name(c: dict) -> str:
    return c.get("name") or c.get("context") or ""


def _stamp(c: dict) -> str:
    return c.get("completedAt") or c.get("startedAt") or c.get("createdAt") or ""


def latest_checks(rollup: list[dict]) -> dict[str, dict]:
    """One entry per check name: the newest attempt. A re-run leaves the old red attempt in the
    rollup next to the new green one; only the newest one is the check's answer."""
    out: dict[str, dict] = {}
    for c in rollup or []:
        n = _name(c)
        if not n:
            continue
        if n not in out or _stamp(c) >= _stamp(out[n]):
            out[n] = c
    return out


def is_sync_merge(headline: str) -> bool:
    """`Merge branch 'main' into ...` / `Merge remote-tracking branch 'origin/main' into ...`:
    auto_update_branch.sh's (or a builder's) catch-up with main. Not a fix to the PR."""
    return headline.startswith("Merge ") and " into " in headline


def last_real_commit(commits: list[dict]) -> dict | None:
    """Newest commit that is actual work on the PR, not a catch-up merge from main."""
    real = [c for c in (commits or []) if not is_sync_merge(c.get("messageHeadline") or "")]
    return real[-1] if real else None


def _commit_time(c: dict | None) -> str:
    if not c:
        return ""
    return c.get("committedDate") or c.get("authoredDate") or ""


def review_findings(pr: dict) -> tuple[bool, str]:
    """(blocked, findings text) from judge-judy's newest verdict comment.

    A BLOCK stands until a real commit lands after it: auto_update_branch.sh merges main into
    red PRs every tick, which moves the head and makes the head-keyed status disappear, but a
    catch-up merge fixes none of the findings (#7975, 2026-09-25: BLOCK at 15:32, head moved by
    a sync merge at 15:50, findings untouched)."""
    comments = [c for c in (pr.get("comments") or []) if isinstance(c, dict)]
    verdicts = [c for c in comments
                if (c.get("body") or "").lstrip().startswith(BLOCK_MARK)
                or any((c.get("body") or "").lstrip().startswith(m) for m in PASS_MARKS)]
    if not verdicts:
        return False, ""
    newest = max(verdicts, key=lambda c: c.get("createdAt") or "")
    body = (newest.get("body") or "").strip()
    if not body.startswith(BLOCK_MARK):
        return False, ""
    fixed_after = _commit_time(last_real_commit(pr.get("commits") or []))
    if fixed_after and fixed_after > (newest.get("createdAt") or ""):
        return False, body  # a real push since the BLOCK: the reviewer has to look again
    return True, body


def classify(pr: dict) -> dict:
    """The one definition of a PR's state. Pure: no I/O."""
    state = (pr.get("state") or "").upper()
    checks = latest_checks(pr.get("statusCheckRollup") or [])
    failed, pending, review_state = [], [], "absent"
    for n, c in sorted(checks.items()):
        if n == REVIEW_CONTEXT:
            s = (c.get("state") or c.get("conclusion") or "").upper()
            review_state = ("failure" if s in RED_STATES or s in RED_CONCLUSIONS
                            else "success" if s == "SUCCESS" else "pending")
            continue
        if c.get("__typename") == "StatusContext" or ("state" in c and "conclusion" not in c):
            s = (c.get("state") or "").upper()
            if s in RED_STATES:
                failed.append(n)
            elif s in ("PENDING", "EXPECTED", ""):
                pending.append(n)
            continue
        status = (c.get("status") or "").upper()
        concl = (c.get("conclusion") or "").upper()
        if status and status != "COMPLETED":
            pending.append(n)
        elif concl in RED_CONCLUSIONS:
            failed.append(n)
        elif concl in DONE_OK:
            pass
        else:
            pending.append(n)

    comment_block, findings = review_findings(pr)
    review_blocked = review_state == "failure" or (review_state != "success" and comment_block)

    if state == "MERGED":
        verdict = "MERGED"
    elif state == "CLOSED":
        verdict = "CLOSED"
    elif failed:
        verdict = "RED"
    elif review_blocked:
        verdict = "BLOCK"
    elif pending or not checks:
        verdict = "PENDING"
    else:
        verdict = "GREEN"

    real = last_real_commit(pr.get("commits") or [])
    return {
        "number": pr.get("number"),
        "state": verdict,
        "head": pr.get("headRefOid") or "",
        "branch": pr.get("headRefName") or "",
        "failed": failed,
        "pending": pending,
        "review": review_state,
        "review_blocked": bool(review_blocked),
        "review_findings": findings if review_blocked else "",
        "armed": pr.get("autoMergeRequest") is not None,
        "last_real_commit": (real or {}).get("oid", ""),
        "last_real_commit_at": _commit_time(real),
        "failed_urls": {n: checks[n].get("detailsUrl") or checks[n].get("targetUrl") or ""
                        for n in failed},
    }


EXIT = {"GREEN": 0, "MERGED": 0, "RED": 1, "BLOCK": 1, "PENDING": 3, "CLOSED": 4}


def _gh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def fetch(pr: int, repo: str | None, gh=_gh) -> dict | None:
    args = ["pr", "view", str(pr), "--json", PR_FIELDS]
    if repo:
        args += ["--repo", repo]
    rc, out = gh(args)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


_ERR_LINE = re.compile(r"(##\[error\]|error|Error|FAILED|RATCHET|STALE |[Ww]ould (be )?reformat|"
                       r"AssertionError|Traceback|E\s{3}| [A-Z]{1,3}\d{3} |^\s*--> )")


def failing_log_excerpt(url: str, repo: str | None, gh=_gh, max_lines: int = 25) -> str:
    """The lines of a failed job's log that say WHY, not the 3,000 lines of setup around them."""
    m = re.search(r"/actions/runs/(\d+)/job/(\d+)", url or "")
    if not m:
        return f"(no Actions job link: {url or 'none'})"
    args = ["run", "view", m.group(1), "--job", m.group(2), "--log-failed"]
    if repo:
        args += ["--repo", repo]
    rc, out = gh(args, timeout=90)
    if rc != 0:
        return f"(log unavailable: {out.strip()[:200]})"
    hits = []
    for line in out.splitlines():
        text = line.split("\t", 2)[-1]
        text = re.sub(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z\s?", "", text)
        if _ERR_LINE.search(text) and "shell: " not in text and "Run " not in text[:6]:
            hits.append(text.rstrip())
    return "\n".join(hits[-max_lines:]) or "\n".join(out.splitlines()[-max_lines:])


def render(info: dict, repo: str | None, gh=_gh, with_logs: bool = True) -> str:
    lines = [f"PR #{info['number']}: {info['state']} (head {info['head'][:12]}, "
             f"auto-merge {'armed' if info['armed'] else 'NOT armed'})"]
    for n in info["failed"]:
        lines.append(f"\n--- FAILED: {n}  {info['failed_urls'].get(n, '')}")
        if with_logs:
            lines.append(failing_log_excerpt(info["failed_urls"].get(n, ""), repo, gh))
    if info["pending"]:
        lines.append(f"\nstill running: {', '.join(info['pending'])}")
    if info["review_blocked"]:
        lines.append(f"\n--- REVIEW BLOCK ({REVIEW_CONTEXT}) -- fix every finding below, push, "
                     "and the reviewer re-reads the new head:\n" + info["review_findings"])
    if info["state"] in ("RED", "BLOCK"):
        lines.append("\nNEXT: fix the above on this PR's own branch, run "
                     "`bash /fleet-kit/scripts/verified_test.sh` (lint autofix + tests + repo "
                     "checks), push, then run this command again.")
    return "\n".join(lines)


def wait(pr: int, repo: str | None, timeout: int, interval: int, gh=_gh,
         sleep=time.sleep, now=time.monotonic) -> dict | None:
    deadline = now() + max(0, timeout)
    while True:
        raw = fetch(pr, repo, gh)
        if raw is None:
            return None
        info = classify(raw)
        if info["state"] != "PENDING" or now() >= deadline:
            return info
        sleep(min(interval, max(1, deadline - now())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", default=os.environ.get("FLEET_REPO_SLUG") or None)
    ap.add_argument("--timeout", type=int, default=540)
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    info = wait(a.pr, a.repo, 0 if a.no_wait else a.timeout, a.interval)
    if info is None:
        print(f"pr_ci_wait: could not read PR #{a.pr}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(info))
    else:
        print(render(info, a.repo))
    return EXIT.get(info["state"], 2)


if __name__ == "__main__":
    sys.exit(main())
