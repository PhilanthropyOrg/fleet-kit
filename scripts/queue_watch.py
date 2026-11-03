#!/usr/bin/env python3
"""queue_watch -- a merge-queue watchdog. Run it from cron every 5 minutes.

Why: on 2026-11-03 the app repo's merge queue sat 6.5 hours with its head entry in
AWAITING_CHECKS and no merge_group run ever created for it, and an UNMERGEABLE entry held the
batch behind it. Nothing merged from about 7:20 AM Central until a person dequeued and
re-enqueued by hand. The arm sweep only arms auto-merge; nothing watched the line itself.

Each pass:
  1. STUCK HEAD. The position-1 entry is AWAITING_CHECKS, no merge_group run exists for its
     current group (created after its enqueuedAt), and it has waited over 15 minutes (since
     enqueuedAt and since this pass first saw that group without a run). Dequeue + re-enqueue.
     At most 2 kicks per PR per 6 hours; the third time it is left dequeued, with one comment.
  2. UNMERGEABLE at any position, seen on two passes in a row: dequeue, with one comment.
  3. NO MERGE: nothing merged to the branch for 90 minutes while 3+ entries wait. One alarm
     line to <log-dir>/queue_watch.alarm and the log, once per episode. Nobody is messaged.
  4. One heartbeat line per pass to <log-dir>/queue_watch.log, Central 12-hour time.

It only ever dequeues, enqueues and comments. It never merges, approves, closes or pushes.
GitHub is read with gh, using GH_TOKEN from the environment or ~/.config/fleet-kit/gh_token.
No token is ever printed.

Usage: queue_watch.py --repo OWNER/NAME --log-dir DIR [--branch main] [--state FILE] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
STUCK_WAIT = timedelta(minutes=15)
UNMERGEABLE_WAIT = timedelta(minutes=4)  # two cron passes, so a passing flicker is not acted on
KICK_WINDOW = timedelta(hours=6)
KICK_LIMIT = 2
NO_MERGE_WAIT = timedelta(minutes=90)
NO_MERGE_MIN_QUEUE = 3

STUCK_COMMENT = ("Merge queue never started checks for this PR twice; removed so the line can "
                 "move. Push or re-arm to try again.")
CONFLICT_COMMENT = ("Merge queue says this PR conflicts with main; removed so the line can move. "
                    "Rebase, then re-arm to try again.")


# --- pure core --------------------------------------------------------------------------------

def _ts(dt: datetime) -> float:
    return dt.timestamp()


def has_run(e: dict, runs: list[dict], branch: str = "main") -> bool:
    """A merge_group run exists for this entry's current group, created after it was enqueued."""
    name = f"gh-readonly-queue/{branch}/pr-{e['pr']}-{e['base_oid']}"
    for r in runs:
        if r["created_at"] < e["enqueued_at"]:
            continue
        if (e["head_oid"] and r["head_sha"] == e["head_oid"]) or (e["base_oid"] and r["head_branch"] == name):
            return True
    return False


def decide(entries: list[dict], runs: list[dict], last_merge_at: datetime | None, state: dict,
           now: datetime, branch: str = "main") -> tuple[list[dict], dict]:
    """(queue, merge_group runs, last merge time, saved state, now) -> (actions, new state).

    Actions: {"kind": "kick"|"dequeue", "pr", "pr_id", "why", "comment": text or None}
             {"kind": "alarm", "pr": None, "line": text}
    """
    now_ts = _ts(now)
    st = {
        "kicks": {k: [t for t in v if now_ts - t < KICK_WINDOW.total_seconds()]
                  for k, v in (state.get("kicks") or {}).items()},
        "commented": dict(state.get("commented") or {}),
        "stuck_seen": {},
        "unmergeable_seen": {},
        "alarm": state.get("alarm"),
    }
    st["kicks"] = {k: v for k, v in st["kicks"].items() if v}
    old_stuck = state.get("stuck_seen") or {}
    old_unm = state.get("unmergeable_seen") or {}
    actions: list[dict] = []

    def drop(e: dict, why: str, text: str) -> None:
        key = f"{e['pr']}:{why}:{e['head_ref_oid']}"
        comment = None if key in st["commented"] else text
        if comment:
            st["commented"][key] = now_ts
        actions.append({"kind": "dequeue", "pr": e["pr"], "pr_id": e["pr_id"], "why": why,
                        "comment": comment})

    # 1. stuck head
    head = next((e for e in entries if e["position"] == 1), None)
    if head and head["state"] == "AWAITING_CHECKS" and not has_run(head, runs, branch):
        key = f"{head['pr']}:{head['head_oid'] or 'none'}"
        first = old_stuck.get(key, now_ts)
        st["stuck_seen"][key] = first
        waited = min(now - head["enqueued_at"], now - datetime.fromtimestamp(first, timezone.utc))
        if waited > STUCK_WAIT:
            del st["stuck_seen"][key]
            pr = str(head["pr"])
            if len(st["kicks"].get(pr, [])) < KICK_LIMIT:
                st["kicks"].setdefault(pr, []).append(now_ts)
                actions.append({"kind": "kick", "pr": head["pr"], "pr_id": head["pr_id"],
                                "why": f"no checks after {int(waited.total_seconds() // 60)}m",
                                "comment": None})
            else:
                drop(head, "stuck", STUCK_COMMENT)

    # 2. unmergeable anywhere
    for e in entries:
        if e["state"] != "UNMERGEABLE":
            continue
        pr = str(e["pr"])
        first = old_unm.get(pr, now_ts)
        if now_ts - first >= UNMERGEABLE_WAIT.total_seconds():
            drop(e, "conflict", CONFLICT_COMMENT)
        else:
            st["unmergeable_seen"][pr] = first

    # 3. no-merge alarm
    if (len(entries) >= NO_MERGE_MIN_QUEUE and last_merge_at is not None
            and now - last_merge_at > NO_MERGE_WAIT):
        if not st["alarm"]:
            st["alarm"] = now_ts
            mins = int((now - last_merge_at).total_seconds() // 60)
            actions.append({"kind": "alarm", "pr": None,
                            "line": f"NO MERGE to {branch} for {mins}m with {len(entries)} in the queue"})
    else:
        st["alarm"] = None

    # keep comment memory for 30 days
    st["commented"] = {k: t for k, t in st["commented"].items() if now_ts - t < 30 * 86400}
    return actions, st


# --- GitHub -----------------------------------------------------------------------------------

QUEUE_QUERY = """
query($owner:String!,$name:String!,$branch:String!){
  repository(owner:$owner,name:$name){
    mergeQueue(branch:$branch){
      entries(first:100){nodes{position state enqueuedAt headCommit{oid} baseCommit{oid}
        pullRequest{number id headRefOid}}}
    }
    pullRequests(states:MERGED, baseRefName:$branch, first:20,
                 orderBy:{field:UPDATED_AT, direction:DESC}){nodes{mergedAt}}
  }
}"""


def _env() -> dict:
    env = dict(os.environ)
    if not env.get("GH_TOKEN"):
        f = Path.home() / ".config/fleet-kit/gh_token"
        if f.is_file():
            env["GH_TOKEN"] = f.read_text().strip()
    env.setdefault("GH_PROMPT_DISABLED", "1")
    return env


def gh(args: list[str], env: dict) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=90, env=env)
    if r.returncode != 0:
        raise OSError(f"gh {args[0]} exit {r.returncode}: {' '.join((r.stderr or '').split())[:200]}")
    return r.stdout


def _dt(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def read_github(repo: str, branch: str, env: dict) -> tuple[list[dict], list[dict], datetime | None]:
    owner, name = repo.split("/", 1)
    data = json.loads(gh(["api", "graphql", "-f", f"query={QUEUE_QUERY}", "-F", f"owner={owner}",
                          "-F", f"name={name}", "-F", f"branch={branch}"], env))
    repo_d = data["data"]["repository"]
    nodes = ((repo_d.get("mergeQueue") or {}).get("entries") or {}).get("nodes") or []
    entries = [{
        "pr": n["pullRequest"]["number"], "pr_id": n["pullRequest"]["id"],
        "position": n["position"], "state": n["state"], "enqueued_at": _dt(n["enqueuedAt"]),
        "head_oid": (n.get("headCommit") or {}).get("oid"),
        "base_oid": (n.get("baseCommit") or {}).get("oid"),
        "head_ref_oid": n["pullRequest"]["headRefOid"],
    } for n in nodes if n.get("pullRequest")]
    merged = [_dt(p["mergedAt"]) for p in repo_d["pullRequests"]["nodes"] if p.get("mergedAt")]
    raw = json.loads(gh(["api", f"repos/{repo}/actions/runs?event=merge_group&per_page=100"], env))
    runs = [{"head_branch": r.get("head_branch") or "", "head_sha": r.get("head_sha") or "",
             "created_at": _dt(r["created_at"])} for r in raw.get("workflow_runs", [])]
    return entries, runs, max(merged) if merged else None


def dequeue(pr_id: str, env: dict) -> None:
    gh(["api", "graphql", "-f",
        "query=mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}",
        "-F", f"id={pr_id}"], env)


def enqueue(pr_id: str, env: dict) -> None:
    gh(["api", "graphql", "-f",
        "query=mutation($id:ID!){enqueuePullRequest(input:{pullRequestId:$id}){clientMutationId}}",
        "-F", f"id={pr_id}"], env)


def comment(repo: str, pr: int, body: str, env: dict) -> None:
    gh(["api", f"repos/{repo}/issues/{pr}/comments", "-f", f"body={body}"], env)


# --- main -------------------------------------------------------------------------------------

def stamp(now: datetime) -> str:
    return now.astimezone(CENTRAL).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--state", default=str(Path.home() / "queue_watch_state.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    log_dir = Path(a.log_dir).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    tag = "queue_watch: dry " if a.dry_run else "queue_watch: "

    def log(line: str, alarm: bool = False) -> None:
        text = f"{stamp(now)} {tag}{line}"
        print(text)
        if a.dry_run:
            return
        with open(log_dir / "queue_watch.log", "a") as f:
            f.write(text + "\n")
        if alarm:
            with open(log_dir / "queue_watch.alarm", "a") as f:
                f.write(text + "\n")

    env = _env()
    try:
        entries, runs, last_merge = read_github(a.repo, a.branch, env)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        log(f"FAIL reading GitHub: {e}")
        return 1

    state_path = Path(a.state).expanduser()
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    actions, new_state = decide(entries, runs, last_merge, state, now, a.branch)

    failed = 0
    for act in actions:
        if act["kind"] == "alarm":
            log(act["line"], alarm=True)
            continue
        what = "kick (dequeue + enqueue)" if act["kind"] == "kick" else "dequeue, leave out"
        if a.dry_run:
            log(f"would {what} #{act['pr']}: {act['why']}{' + comment' if act['comment'] else ''}")
            continue
        try:
            dequeue(act["pr_id"], env)
            if act["kind"] == "kick":
                enqueue(act["pr_id"], env)
            if act["comment"]:
                comment(a.repo, act["pr"], act["comment"], env)
            log(f"{what} #{act['pr']}: {act['why']}{' + comment' if act['comment'] else ''}")
        except (OSError, subprocess.SubprocessError) as e:
            failed += 1
            log(f"FAIL {what} #{act['pr']}: {e}")

    if not a.dry_run:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state, indent=1, sort_keys=True))
        tmp.replace(state_path)

    head = next((e for e in entries if e["position"] == 1), None)
    head_txt = (f"head #{head['pr']} {head['state']} run={'yes' if has_run(head, runs, a.branch) else 'no'}"
                if head else "head none")
    unm = sum(1 for e in entries if e["state"] == "UNMERGEABLE")
    since = f"{int((now - last_merge).total_seconds() // 60)}m ago" if last_merge else "unknown"
    log(f"ok queue={len(entries)} {head_txt} unmergeable={unm} last_merge={since} "
        f"actions={len(actions)}{' failed=' + str(failed) if failed else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
