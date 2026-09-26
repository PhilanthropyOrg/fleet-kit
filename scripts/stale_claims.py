#!/usr/bin/env python3
"""stale_claims.py -- fleet:claimed is a LEASE, not a deed.

THE GAP (philanthropy, 2026-09-25). gru's 13:38 UTC pass claimed 8 fleet:reif-priority items
(#7937-#7942, #7948, #7950) for one minion batch. The minion's work died; only #7982 and #7986
came out, and fleet:claimed stayed on the rest for 12 hours. Every later gru pass filtered them
out as claimed and reported "no open item passed both gates" -- the top tier of the backlog sat
idle behind a label nobody was holding, until M removed it by hand at ~01:30 UTC 09-26.
worktree_builder.sh releases on its own failure path, but a batch minion that is killed, times
out, or builds only part of its batch had no release path at all.

The rule: a claim is held only while something is visibly working it. At the start of every
gru pass (run_gru_fanout.sh, before the model starts) each open fleet:claimed issue is RELEASED
when ALL of these hold for FLEET_CLAIM_LEASE_MIN (60) minutes:
  - the claim itself (its newest `claimed-by:` comment) is older than the lease
  - no live run_member.sh / worktree_builder.sh / dispatch_fixer.sh process names the item
    (--item/--items), or names an open PR that references it (the-fixer --item <PR>)
  - no open PR that references it (branch `-item<n>_...` or a word-bounded #n in title/body)
    has a REAL commit inside the lease (auto_update_branch's merges of main do not count --
    pr_ci_wait.last_real_commit, the same definition red_prs.py uses)
  - no pushed branch naming the item has a commit inside the lease
A claim marie deliberately left in place (a merged PR references it, marie.md Part A) and any
fleet:needs-close-verify item are never released here: those are held for a close/verify pass,
not for a builder, until a newer claimed-by supersedes marie's note.

  stale_claims.py release [--dry-run] [--json]   sweep; releases, comments, logs
  stale_claims.py last                           the newest sweep's result (gru step 0 reads it)

Every release comments on the issue (why, and the evidence checked) and appends one line to
$FLEET_LOG_DIR/claim_leases.jsonl. The sweep's result, including how many ELIGIBLE items are
still held as claimed, is written to $FLEET_LOG_DIR/claim_leases_last.json for gru's report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_github  # noqa: E402 -- one definition of the release commands
import minion_checkpoint  # noqa: E402 -- one definition of a checkpoint PR
import pr_ci_wait  # noqa: E402 -- one definition of a "real" commit

LABEL_CLAIMED = board_github.LABEL_CLAIMED
PREFIX = board_github.PREFIX
CLOSE_VERIFY = f"{PREFIX}needs-close-verify"
HUMAN_BLOCKED = board_github.LABEL_HUMAN_BLOCKED
PRIORITY_LABEL = os.environ.get("FLEET_REIF_PRIORITY_LABEL", f"{PREFIX}reif-priority")
TIER_LABELS = {f"{PREFIX}priority-{t}" for t in ("high", "medium", "low")}
MARIE_HOLD = "fleet:claimed left in place"
RUNNERS = ("run_member.sh", "worktree_builder.sh", "dispatch_fixer.sh")
ITEMS_IN_BRANCH = re.compile(r"-item(\d+(?:_\d+)*)")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


LEASE_MIN = _env_int("FLEET_CLAIM_LEASE_MIN", 60)
# A claim whose only activity is a minion checkpoint draft PR, with no live runner, is released
# after this many minutes instead of the full lease (2026-09-26: #7937/#7938/#7941 sat claimed
# behind drafts #8152/#8134/#8136 with no minion running). The checkpoint's own commits are the
# dead pass saving its work, not someone working. The next gru pass resumes the branch
# (run_member.sh -> minion_checkpoint.py find). The grace covers the claim-to-spawn gap.
CHECKPOINT_GRACE_MIN = _env_int("FLEET_CLAIM_CHECKPOINT_GRACE_MIN", 10)


def log_dir() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def items_of(branch: str) -> set[int]:
    m = ITEMS_IN_BRANCH.search(branch or "")
    return {int(n) for n in m.group(1).split("_")} if m else set()


def mentions(text: str, n: int) -> bool:
    return re.search(rf"(?<![\w/])(?:gh)?#0*{n}(?!\d)", text or "", re.I) is not None


# --- live processes ---------------------------------------------------------------------------

def numbers_in_argv(argv: list[str]) -> set[int]:
    """Issue/PR numbers a runner command line names via --item N / --items a,b / --item=N, or
    positionally (dispatch_fixer.sh <PR> <PR> ...). Pure."""
    if not argv or not any(a.endswith(RUNNERS) for a in argv[:3]):
        return set()
    out: set[int] = set()
    positional = any(a.endswith("dispatch_fixer.sh") for a in argv[:3])
    for i, a in enumerate(argv):
        val = None
        if a in ("--item", "--items") and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith(("--item=", "--items=")):
            val = a.split("=", 1)[1]
        elif positional and a.isdigit():
            val = a
        if val:
            out |= {int(x) for x in re.split(r"[,_\s]+", val) if x.isdigit()}
    return out


def live_numbers(proc: Path = Path("/proc")) -> set[int]:
    """Every number a live runner process on this box (this container's pid namespace) names.
    Zombies have an empty cmdline and so never count."""
    out: set[int] = set()
    for d in proc.glob("[0-9]*"):
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            out |= numbers_in_argv(raw.decode(errors="replace").rstrip("\0").split("\0"))
    return out


# --- the decision (pure) ----------------------------------------------------------------------

def claim_time(issue: dict) -> float | None:
    ts = [_ts(c.get("createdAt")) for c in issue.get("comments") or []
          if (c.get("body") or "").lstrip().startswith("claimed-by:")]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


def marie_hold_time(issue: dict) -> float | None:
    ts = [_ts(c.get("createdAt")) for c in issue.get("comments") or []
          if MARIE_HOLD in (c.get("body") or "")]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


def labels(issue: dict) -> set[str]:
    return {lb.get("name") for lb in issue.get("labels") or [] if isinstance(lb, dict)}


def is_eligible(issue: dict) -> bool:
    """Would gru's step 2a/2b pick this up if it were not claimed?"""
    lb = labels(issue)
    if HUMAN_BLOCKED in lb:
        return False
    return PRIORITY_LABEL in lb or (f"{PREFIX}backlog" in lb and bool(lb & TIER_LABELS))


def assess(issue: dict, prs: list[dict], branches: list[dict], live: set[int], now: float,
           lease_min: int = LEASE_MIN) -> dict:
    """One claimed issue -> {"number", "release": bool, "why", "evidence"}. Pure.

    prs: open PRs as {"number", "headRefName", "title", "body", "last_real_at" (epoch|None),
    "createdAt"}. branches: {"name", "committed_at" (epoch)} for pushed branches with no open PR.
    """
    n = int(issue["number"])
    lease = lease_min * 60
    lb = labels(issue)
    claimed_at = claim_time(issue) or _ts(issue.get("updatedAt"))
    ev = {"claimed_at": _iso(claimed_at), "lease_min": lease_min}

    def hold(kind: str, why: str) -> dict:
        return {"number": n, "release": False, "kind": kind, "why": why, "evidence": ev}

    if CLOSE_VERIFY in lb:
        return hold("close-verify", f"{CLOSE_VERIFY}: held for a close/verify pass")
    mh = marie_hold_time(issue)
    if mh and (not claimed_at or mh >= claimed_at):
        return hold("marie-merged-pr", "marie left the claim in place (a merged PR references it)")
    mine = [p for p in prs if _refs(p, n)]
    ev["open_prs"] = [p["number"] for p in mine]
    idle = n not in live and not any(int(p["number"]) in live for p in mine)
    ckpt = [p["number"] for p in mine
            if minion_checkpoint.is_checkpoint_pr(p.get("title"), p.get("body"))]
    if idle and ckpt and (not claimed_at or now - claimed_at >= CHECKPOINT_GRACE_MIN * 60):
        ev["checkpoint_prs"] = ckpt
        return {"number": n, "release": True, "kind": "checkpoint-idle", "evidence": ev,
                "why": (f"checkpoint draft PR #{ckpt[0]} holds the work and no live runner is on "
                        f"it -- the next minion resumes that branch"
                        f" (claimed {_iso(claimed_at) or 'unknown'})")}
    if claimed_at and now - claimed_at < lease:
        return hold("inside-lease", f"claimed {int((now - claimed_at) // 60)} min ago (inside the lease)")

    if n in live:
        return hold("live-runner", "a live runner process names this item")
    busy = [p["number"] for p in mine if int(p["number"]) in live]
    if busy:
        return hold("live-runner", f"a live runner process is working its PR #{busy[0]}")
    for p in mine:
        t = p.get("last_real_at") or _ts(p.get("createdAt"))
        if t and now - t < lease:
            return hold("pr-activity", f"open PR #{p['number']} had a real commit {int((now - t) // 60)} min ago")
    for b in branches:
        if n in items_of(b.get("name") or "") and now - float(b.get("committed_at") or 0) < lease:
            return hold("branch-activity", f"branch {b['name']} had a commit {int((now - b['committed_at']) // 60)} min ago")
    newest = max([p.get("last_real_at") or _ts(p.get("createdAt")) or 0 for p in mine] +
                 [float(b.get("committed_at") or 0) for b in branches
                  if n in items_of(b.get("name") or "")] + [0])
    ev["last_activity"] = _iso(newest) if newest else None
    return {"number": n, "release": True, "kind": "expired", "evidence": ev,
            "why": (f"no live runner and no PR/branch activity for {lease_min}+ min"
                    f" (last activity {_iso(newest) or 'none'}; claimed {_iso(claimed_at) or 'unknown'})")}


def release_note(row: dict) -> str:
    prs = row["evidence"].get("open_prs") or []
    merged = row["evidence"].get("merged_prs") or []
    tail = (f" Open PR(s) {', '.join('#%d' % p for p in prs)} still reference it -- the next build "
            "should read them first and build on, not beside, them.") if prs else ""
    if merged:
        # A batch minion writes "Part of #N. Remaining: ..." for what it did not finish (#7982
        # built part of #7940 and #7948, merged, and left both open on purpose).
        tail += (f" Merged PR(s) {', '.join('#%d' % p for p in merged)} reference it -- read their "
                 "`Remaining:` line and build only what remains, or close it if nothing does.")
    if row.get("kind") == "checkpoint-idle":
        return (f"stale_claims: released fleet:claimed -- {row['why']}. "
                f"Re-claimable by the next gru pass.{tail}")
    return (f"stale_claims: released fleet:claimed -- lease expired: {row['why']}. "
            f"Re-claimable by the next gru pass.{tail}")


# --- gh ---------------------------------------------------------------------------------------

def _repo_args(repo: str | None) -> list[str]:
    return ["--repo", repo] if repo else []


def claimed_issues(repo: str | None, gh=pr_ci_wait._gh) -> list[dict] | None:
    rc, out = gh(["issue", "list", "--state", "open", "--label", LABEL_CLAIMED, "--limit", "500",
                  "--json", "number,title,labels,comments,updatedAt", *_repo_args(repo)], timeout=180)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def open_prs(repo: str | None, claimed: set[int], gh=pr_ci_wait._gh) -> list[dict] | None:
    rc, out = gh(["pr", "list", "--state", "open", "--limit", "200", "--json",
                  "number,headRefName,title,body,createdAt", *_repo_args(repo)], timeout=120)
    if rc != 0:
        return None
    try:
        light = json.loads(out)
    except json.JSONDecodeError:
        return None
    wanted = [p for p in light if items_of(p.get("headRefName") or "") & claimed
              or any(mentions(f"{p.get('title') or ''}\n{p.get('body') or ''}", n) for n in claimed)]
    from concurrent.futures import ThreadPoolExecutor

    def real(p: dict) -> dict:
        full = pr_ci_wait.fetch(int(p["number"]), repo, gh) or {}
        c = pr_ci_wait.last_real_commit(full.get("commits") or [])
        return {**p, "last_real_at": _ts(pr_ci_wait._commit_time(c)) if c else None}

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(real, wanted))


def merged_prs(repo: str | None, claimed: set[int], gh=pr_ci_wait._gh) -> list[dict]:
    """Recently merged PRs that reference a claimed item. Informational only (named in the
    release comment), so a failed read is just an empty list."""
    rc, out = gh(["pr", "list", "--state", "merged", "--limit", "100", "--json",
                  "number,headRefName,title,body", *_repo_args(repo)], timeout=120)
    try:
        light = json.loads(out) if rc == 0 else []
    except json.JSONDecodeError:
        return []
    return [p for p in light if items_of(p.get("headRefName") or "") & claimed
            or any(mentions(f"{p.get('title') or ''}\n{p.get('body') or ''}", n) for n in claimed)]


def _refs(p: dict, n: int) -> bool:
    return n in items_of(p.get("headRefName") or "") or \
        mentions(f"{p.get('title') or ''}\n{p.get('body') or ''}", n)


def pushed_branches(repo: str | None, skip: set[str], gh=pr_ci_wait._gh) -> list[dict]:
    owner, _, name = (repo or "").partition("/")
    if not name:
        rc, out = gh(["repo", "view", "--json", "owner,name"])
        try:
            d = json.loads(out) if rc == 0 else {}
            owner, name = d["owner"]["login"], d["name"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
    q = ('query($o:String!,$n:String!){repository(owner:$o,name:$n){refs(refPrefix:"refs/heads/",'
         'query:"-item",first:100,orderBy:{field:TAG_COMMIT_DATE,direction:DESC}){nodes{name '
         'target{... on Commit{committedDate}}}}}}')
    rc, out = gh(["api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"n={name}"])
    try:
        nodes = json.loads(out)["data"]["repository"]["refs"]["nodes"] if rc == 0 else []
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    return [{"name": b["name"], "committed_at": _ts((b.get("target") or {}).get("committedDate")) or 0}
            for b in nodes if b.get("name") not in skip]


def sweep(repo: str | None, dry_run: bool, gh=pr_ci_wait._gh, now: float | None = None,
          live: set[int] | None = None, run=None) -> dict:
    now = time.time() if now is None else now
    issues = claimed_issues(repo, gh)
    if issues is None:
        return {"error": "gh issue list failed -- stale claims unknown this pass", "ts": _iso(now)}
    nums = {int(i["number"]) for i in issues}
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=4)  # gh in the container: ~30-50s per list call
    merged_f = pool.submit(merged_prs, repo, nums, gh)
    prs = open_prs(repo, nums, gh)
    if prs is None:
        # Without the PR list a live PR looks like no activity: releasing blind could hand a
        # claim with a working PR to a second builder. Hold everything this pass.
        return {"error": "gh pr list failed -- no claims released this pass", "ts": _iso(now)}
    branches = pushed_branches(repo, {p["headRefName"] for p in prs}, gh)
    live = live_numbers() if live is None else live
    rows = [assess(i, prs, branches, live, now) for i in issues]
    merged = merged_f.result()
    by_num = {int(i["number"]): i for i in issues}
    released, failed = [], []

    def release(r: dict) -> bool:
        ok = True
        for cmd in board_github.build_release_cmds(r["number"], release_note(r)):
            rc, out = (run or _run)(cmd + _repo_args(repo))
            if rc != 0:
                ok = False
                print(f"stale_claims: release #{r['number']} step FAILED: {out[:200]}", file=sys.stderr)
        _log({"ts": _iso(time.time()), "event": "released" if ok else "release_failed",
              "number": r["number"], "why": r["why"], "evidence": r["evidence"]})
        return ok

    due = [r for r in rows if r["release"]]
    for r in due:
        r["evidence"]["merged_prs"] = [p["number"] for p in merged if _refs(p, r["number"])]
    if dry_run:
        released = [r["number"] for r in due]
    else:
        # In parallel: serially, 17 releases at ~15s of gh each ran the first live sweep
        # (2026-09-26 02:15 UTC) past run_gru_fanout.sh's timeout after 5.
        for r, ok in zip(due, pool.map(release, due)):
            (released if ok else failed).append(r["number"])
    pool.shutdown(wait=False)
    held = [r for r in rows if not r["release"] or r["number"] in failed]
    eligible_held = [r for r in held if is_eligible(by_num[r["number"]])]
    by_kind: dict[str, list[int]] = {}
    for r in eligible_held:
        by_kind.setdefault("release-failed" if r["release"] else r["kind"], []).append(r["number"])
    return {
        "ts": _iso(now), "dry_run": dry_run, "lease_min": LEASE_MIN,
        "claimed": len(rows),
        "released": sorted(released),
        "release_failed": sorted(failed),
        "released_eligible": sorted(n for n in released if is_eligible(by_num[n])),
        "held_eligible": sorted(r["number"] for r in eligible_held),
        "held_eligible_by_reason": {k: sorted(v) for k, v in sorted(by_kind.items())},
        "held": [{"number": r["number"], "kind": r["kind"], "why": r["why"]} for r in held],
    }


def _run(cmd: list[str]) -> tuple[int, str]:
    return board_github._run(cmd)


def _log(entry: dict) -> None:
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        with open(log_dir() / "claim_leases.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"stale_claims: could not log: {e}", file=sys.stderr)


def summary(res: dict) -> str:
    if res.get("error"):
        return f"stale_claims: {res['error']}"
    fmt = lambda ns: " ".join(f"#{n}" for n in ns) or "none"  # noqa: E731
    return (f"stale_claims{' (dry run)' if res.get('dry_run') else ''}: {res['claimed']} claimed, "
            f"released {len(res['released'])} ({fmt(res['released'])}); "
            f"{len(res['held_eligible'])} eligible items still held as claimed -- "
            + ("; ".join(f"{k} {len(v)} ({fmt(v)})" for k, v in res["held_eligible_by_reason"].items())
               or "none"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="release fleet:claimed leases nobody is holding")
    ap.add_argument("cmd", choices=["release", "last"])
    ap.add_argument("--repo", default=os.environ.get("FLEET_REPO_SLUG") or None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    last = log_dir() / "claim_leases_last.json"
    if a.cmd == "last":
        try:
            res = json.loads(last.read_text())
        except (OSError, json.JSONDecodeError):
            print(json.dumps({"error": f"no sweep result at {last} -- the pass-start sweep did not run"}))
            return 2
        print(json.dumps(res, indent=1))
        return 0
    res = sweep(a.repo, a.dry_run)
    if not a.dry_run:
        try:
            last.parent.mkdir(parents=True, exist_ok=True)
            last.write_text(json.dumps(res, indent=1))
        except OSError as e:
            print(f"stale_claims: could not write {last}: {e}", file=sys.stderr)
    print(json.dumps(res, indent=1) if a.json else summary(res))
    return 2 if res.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
