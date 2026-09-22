#!/usr/bin/env python3
"""sentry_deploy_task.py -- build sentry's --task string from what just shipped.

WHY (gh#1217, Reif: "sentry gets webhooked on every deploy, hits the most recent set of items
that were built, derives only what the user's job to do is, and gives itself the task itself
-- and then spends the rest of the turn looking for other errors"). deploy.sh's finish_deploy()
already kicks one sentry pass within seconds of every cutover (gh#663) -- confirmed live, this
already fires. But that kick is generic (`run_member.sh sentry`, no --task), so sentry has no
idea which PR(s) just shipped and re-runs its full untargeted pass instead of checking the
specific job those PRs were meant to enable first.

Queries the PRODUCT repo (the one mounted at /repo inside the container, not fleet-kit's own)
for PRs merged in the last `--window-s` seconds (default 10800 = sentry's own cron interval,
member/sentry/sentry.fleet.json's schedule.interval_s -- a window matching sentry's own cadence
means "since sentry last would have looked anyway", not an arbitrary number). Prints a --task
string naming each PR's number/title/body-first-line, capped at --max-prs (a big auto-merge
batch must not eat sentry's whole pass on step 0 alone -- gh#1217's own stated risk) with the
overflow said explicitly rather than silently dropped.

Prints NOTHING (exit 0, empty stdout) when: no PRs merged in the window, `gh` fails, or the
repo has no merged-PR concept reachable (a config-only/infra deploy). The caller's contract:
empty stdout means "fall back to the plain generic kick", same fail-open shape as every other
maxx/budget reader in this kit -- an unreadable signal here must only ever mean "sentry gets no
extra task", never "block the kick" or "invent a fake one".

Usage: sentry_deploy_task.py [--repo /repo] [--window-s 10800] [--max-prs 5]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def _iso_ago(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def fetch_recent_merged_prs(repo: str, window_s: int, limit: int = 30) -> list[dict]:
    """Returns merged PRs in the window, newest first, or [] on any failure. Never raises --
    this is a best-effort signal for an ad-hoc --task, not a correctness-critical read."""
    since = _iso_ago(window_s)
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--search", f"merged:>={since}",
             "--json", "number,title,body,mergedAt", "--limit", str(limit)],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(prs, list):
        return []
    prs.sort(key=lambda p: p.get("mergedAt") or "", reverse=True)
    return prs


def _first_line(text: str | None, max_len: int = 200) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line[:max_len]


def build_task(prs: list[dict], max_prs: int) -> str:
    """Empty string if prs is empty. Otherwise a --task instruction naming each PR (capped at
    max_prs, overflow stated) and telling sentry to derive+verify the job before its normal
    pass."""
    if not prs:
        return ""
    shown = prs[:max_prs]
    overflow = len(prs) - len(shown)
    lines = []
    for pr in shown:
        num = pr.get("number")
        title = (pr.get("title") or "").strip()
        summary = _first_line(pr.get("body"))
        entry = f"#{num}: {title}"
        if summary and summary != title:
            entry += f" -- {summary}"
        lines.append(entry)
    prs_block = "\n".join(f"  {l}" for l in lines)
    overflow_note = ""
    if overflow > 0:
        overflow_note = (
            f"\n({overflow} additional PR(s) also merged in this window, not listed -- "
            "do not try to derive a job for all of them; the ones above are enough for this pass.)"
        )
    return (
        "These PR(s) just deployed:\n"
        f"{prs_block}{overflow_note}\n\n"
        "Before your normal pass, for each PR above derive in one sentence what person and "
        "what job it was meant to enable (same discipline as the human-intent verification "
        "gate: write what the PERSON accomplishes, then exercise it end-to-end as they would). "
        "Verify that specific job now, live, the way a real person would -- not the mechanism, "
        "the job. Report each one as its own finding (pass or fail), distinct from your normal "
        "crawl findings. Only after that, continue your normal steps 1-9."
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/repo")
    parser.add_argument("--window-s", type=int, default=10800)
    parser.add_argument("--max-prs", type=int, default=5)
    args = parser.parse_args(argv[1:])

    prs = fetch_recent_merged_prs(args.repo, args.window_s)
    task = build_task(prs, args.max_prs)
    if task:
        print(task)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
