#!/usr/bin/env python3
"""red_prs.py -- the fleet's own red/blocked PRs, and who gets sent to fix them.

THE GAP (2026-09-25). philanthropy's merge-stall alarm (auto-merge-open-prs.yml `stall-alarm`,
#7943) only sees GREEN PRs that do not merge. A RED one was nobody's: the minion that built it
had ended its pass, gru's step 2a sent every pass to NEW fleet:reif-priority items first, and
the-fixer's --item sub-passes were muted by the parent's own dedup (#8002). #7975, #7982 and
#7986 -- all builds of Reif-priority items -- sat red for hours while gru built more. That
alarm cannot simply grow a red branch: the repo-health ratchet freezes workflow YAML at its
line count (2,531), and a GitHub Action has no way to put a fixer on the box anyway. So the
red half of the stall detector lives here, next to the one member that acts on it.

  red_prs.py list  [--json]           every open fleet PR that is RED or review-BLOCKed
  red_prs.py due   [--limit N]        the ones gru's step 0 dispatches a fixer to THIS pass:
                                      a Reif-priority item's PR as soon as it is red, any other
                                      fleet PR once it has been red with no real push for
                                      FLEET_RED_PR_STALL_MIN (60) minutes
  red_prs.py claim <pr>               the dedup gate every fixer dispatch goes through
                                      (run_member.sh calls it for `the-fixer --item <pr>`):
                                      exit 0 = go (recorded), exit 1 = already sent for this
                                      exact content recently, or given up on it

Dedup is keyed on the PR's last REAL commit, not its head: auto_update_branch.sh merges main
into open PRs every tick, which moves the head without fixing anything. A new real push resets
the attempt count; the same content gets FLEET_RED_PR_MAX_ATTEMPTS (3) fixer passes, at most one
per FLEET_RED_PR_REDISPATCH_MIN (45) minutes, then shows as `exhausted` for a human-readable
report instead of being re-sent every hour.

A "fleet PR" is one on a `member/...` or `minion/...` branch -- the convention run_member.sh
stamps and auto_update_branch.sh already keys on. A human's branch is never touched.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_ci_wait  # noqa: E402 -- one definition of "red" for detector, router and fixer

FLEET_BRANCH = re.compile(r"^(member|minion)/")
ITEMS_IN_BRANCH = re.compile(r"-item(\d+(?:_\d+)*)")
# `commits`/`comments` on a 60-PR list call exceeds GitHub's GraphQL node limit ("requesting up
# to 600,000 possible nodes"), so the list is light and each fleet PR is then read in full.
LIST_FIELDS = "number,headRefName,isDraft"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


STALL_MIN = _env_int("FLEET_RED_PR_STALL_MIN", 60)
REDISPATCH_MIN = _env_int("FLEET_RED_PR_REDISPATCH_MIN", 45)
MAX_ATTEMPTS = _env_int("FLEET_RED_PR_MAX_ATTEMPTS", 3)
PRIORITY_LABEL = os.environ.get("FLEET_REIF_PRIORITY_LABEL", "fleet:reif-priority")


def ledger_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "red_pr_dispatch.json"


def items_of(branch: str) -> list[int]:
    m = ITEMS_IN_BRANCH.search(branch or "")
    return [int(n) for n in m.group(1).split("_")] if m else []


def _ts(iso: str) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def describe(pr: dict, priority_issues: set[int], now: float) -> dict | None:
    """One red/blocked fleet PR as a row, or None if it is not one. Pure."""
    if not FLEET_BRANCH.match(pr.get("headRefName") or "") or pr.get("isDraft"):
        return None
    info = pr_ci_wait.classify(pr)
    if info["state"] not in ("RED", "BLOCK"):
        return None
    items = items_of(pr.get("headRefName") or "")
    labels = {lb.get("name") for lb in pr.get("labels") or [] if isinstance(lb, dict)}
    reif = PRIORITY_LABEL in labels or any(i in priority_issues for i in items)
    since = _ts(info["last_real_commit_at"]) or _ts(pr.get("createdAt") or "") or now
    quiet_min = int((now - since) // 60)
    return {
        "number": pr.get("number"),
        "state": info["state"],
        "failed": info["failed"],
        "review_blocked": info["review_blocked"],
        "items": items,
        "reif_priority": reif,
        "minutes_since_real_push": quiet_min,
        "stalled": quiet_min >= STALL_MIN,
        "content": info["last_real_commit"] or info["head"],
        "url": pr.get("url") or "",
    }


# --- the dispatch ledger -------------------------------------------------------------------

@contextlib.contextmanager
def _locked_ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        box = {"data": data}
        yield box
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(box["data"], indent=1, sort_keys=True))
        tmp.replace(path)


def verdict(entry: dict | None, content: str, now: float) -> str:
    """'go', 'recent' or 'exhausted' for sending one more fixer at this content. Pure."""
    if not entry or entry.get("content") != content:
        return "go"
    if int(entry.get("attempts", 0)) >= MAX_ATTEMPTS:
        return "exhausted"
    if now - float(entry.get("last", 0)) < REDISPATCH_MIN * 60:
        return "recent"
    return "go"


def record(ledger: dict, pr: int, content: str, now: float) -> dict:
    entry = ledger.get(str(pr))
    if not entry or entry.get("content") != content:
        entry = {"content": content, "attempts": 0}
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last"] = now
    ledger[str(pr)] = entry
    return entry


def plan(rows: list[dict], ledger: dict, now: float, limit: int) -> dict:
    """Which red PRs get a fixer this pass. Reif-priority first, then oldest quiet. Pure."""
    due, held, exhausted = [], [], []
    wanted = [r for r in rows if r["reif_priority"] or r["stalled"]]
    wanted.sort(key=lambda r: (0 if r["reif_priority"] else 1, -r["minutes_since_real_push"]))
    for r in wanted:
        v = verdict(ledger.get(str(r["number"])), r["content"], now)
        if v == "go" and len(due) < limit:
            due.append(r)
        elif v == "exhausted":
            exhausted.append(r["number"])
        else:
            held.append({"number": r["number"], "why": v if v != "go" else "over this pass's limit"})
    return {"due": due, "held": held, "exhausted": exhausted,
            "not_yet_stalled": [r["number"] for r in rows if r not in wanted]}


# --- gh ---------------------------------------------------------------------------------------

def _open_prs(repo: str | None, gh=pr_ci_wait._gh) -> list[dict] | None:
    args = ["pr", "list", "--state", "open", "--limit", "60", "--json", LIST_FIELDS]
    if repo:
        args += ["--repo", repo]
    rc, out = gh(args, timeout=120)
    if rc != 0:
        return None
    try:
        light = json.loads(out)
    except json.JSONDecodeError:
        return None
    wanted = [int(p["number"]) for p in light
              if not p.get("isDraft") and FLEET_BRANCH.match(p.get("headRefName") or "")]
    # In parallel: one `gh pr view` per fleet PR serially took >120s in the container on
    # 2026-09-25 and gru's Bash call was backgrounded mid-step.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        got = list(ex.map(lambda n: pr_ci_wait.fetch(n, repo, gh), wanted))
    return [p for p in got if p is not None]


def _priority_issues(repo: str | None, gh=pr_ci_wait._gh) -> set[int]:
    args = ["issue", "list", "--state", "open", "--label", PRIORITY_LABEL, "--limit", "300",
            "--json", "number"]
    if repo:
        args += ["--repo", repo]
    rc, out = gh(args)
    try:
        return {int(i["number"]) for i in json.loads(out)} if rc == 0 else set()
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()


def rows_now(repo: str | None, gh=pr_ci_wait._gh, now: float | None = None) -> list[dict] | None:
    prs = _open_prs(repo, gh)
    if prs is None:
        return None
    pri = _priority_issues(repo, gh)
    t = time.time() if now is None else now
    return [r for r in (describe(p, pri, t) for p in prs) if r]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the fleet's red PRs and their fixer dispatch")
    ap.add_argument("cmd", choices=["list", "due", "claim"])
    ap.add_argument("pr", nargs="?", type=int)
    ap.add_argument("--repo", default=os.environ.get("FLEET_REPO_SLUG") or None)
    ap.add_argument("--limit", type=int, default=_env_int("FLEET_GRU_MAX_FIXERS", 6))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = time.time()

    if a.cmd == "claim":
        if not a.pr:
            ap.error("claim needs a PR number")
        raw = pr_ci_wait.fetch(a.pr, a.repo)
        if raw is None:
            print(f"red_prs: could not read PR #{a.pr} -- allowing the dispatch (fail open)")
            return 0
        info = pr_ci_wait.classify(raw)
        if info["state"] not in ("RED", "BLOCK"):
            print(f"red_prs: PR #{a.pr} is {info['state']} -- not a red-PR dispatch, not recorded")
            return 0
        content = info["last_real_commit"] or info["head"]
        with _locked_ledger(ledger_path()) as box:
            v = verdict(box["data"].get(str(a.pr)), content, now)
            if v != "go":
                e = box["data"].get(str(a.pr), {})
                print(f"red_prs: SKIP PR #{a.pr} -- {v}: {e.get('attempts')} fixer pass(es) already "
                      f"sent for content {content[:12]}, last {int((now - e.get('last', now)) // 60)} min ago")
                return 1
            e = record(box["data"], a.pr, content, now)
        print(f"red_prs: dispatch PR #{a.pr} ({info['state']}), attempt {e['attempts']}/{MAX_ATTEMPTS} "
              f"at content {content[:12]}")
        return 0

    rows = rows_now(a.repo)
    if rows is None:
        print(json.dumps({"error": "gh pr list failed -- red PRs unknown this pass"}))
        return 2
    if a.cmd == "list":
        print(json.dumps(rows, indent=1) if a.json else
              "\n".join(f"#{r['number']} {r['state']} {'REIF ' if r['reif_priority'] else ''}"
                        f"quiet {r['minutes_since_real_push']}m failed={','.join(r['failed']) or '-'}"
                        f"{' review-BLOCK' if r['review_blocked'] else ''}" for r in rows) or "none")
        return 0
    with _locked_ledger(ledger_path()) as box:
        out = plan(rows, box["data"], now, max(0, a.limit))
    out["due"] = [{k: r[k] for k in ("number", "state", "failed", "review_blocked", "items",
                                     "reif_priority", "minutes_since_real_push")} for r in out["due"]]
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
