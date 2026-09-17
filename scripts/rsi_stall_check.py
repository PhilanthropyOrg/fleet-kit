#!/usr/bin/env python3
"""rsi_stall_check -- the Magikarp score is graded every 3h; nobody was READING the grade.

THE GAP (fk#1122, Reif 2026-09-17: "make sure the fleet can still achieve rsi"). fleet-kit
PR #1116 set jefe, dumbledore and vp to enabled:false. dumbledore was the only member whose
mandate was "find what is rotting and fix it at the layer that produced it" -- the member
that read self_improve_score.jsonl and acted on a flat or falling score. With it off, the
grader still runs (self_improve_score.sh, hourly cron) and the score sat at 52-55 for five
straight ticks on dino (03:07Z-15:07Z, 2026-09-17) with the same 3 hits / 4 misses / 8 open
ledger rows every time. Grading happened; nothing closed the loop.

This is the mechanical replacement, no model: read the ledger, and if the last N scores
never rose, file ONE fleet:priority-high issue in fleet-kit's own repo naming the stall and
the grader's own reasoning lines, so marie ranks it and minion builds it. Deduped on the
open-issue title (same pattern as reif_eyes.py), so a stall that is already filed is a no-op
until that issue closes.

Run: cron, hourly (entrypoint.sh). Needs FLEET_LOG_DIR; KIT_REPO_SLUG or a git remote at the
kit root for the slug. `--dry-run` prints the decision and files nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
LABELS = "fleet:backlog,fleet:priority-high"
TITLE_PREFIX = "RSI stalled: Magikarp score"
DEFAULT_TICKS = 3


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def read_scores(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec.get("score"), (int, float)):
            rows.append(rec)
    return rows


def stall(rows: list[dict], ticks: int = DEFAULT_TICKS) -> dict | None:
    """The last `ticks` scores never rose: each is <= the one before it. Returns the
    evidence dict, or None when the score rose at least once inside the window (or there
    are not yet enough ticks to say anything)."""
    if len(rows) < ticks:
        return None
    tail = rows[-ticks:]
    scores = [r["score"] for r in tail]
    if any(b > a for a, b in zip(scores, scores[1:])):
        return None
    return {"scores": scores, "latest": tail[-1],
            "reasons": [str(r.get("reasoning") or "")[:300] for r in tail]}


def kit_slug() -> str:
    env = os.environ.get("KIT_REPO_SLUG")
    if env:
        return env
    r = _run(["git", "-C", str(KIT_DIR), "remote", "get-url", "origin"])
    url = (r.stdout or "").strip()
    for pre in ("git@github.com:", "https://github.com/"):
        if url.startswith(pre):
            url = url[len(pre):]
    return url[:-4] if url.endswith(".git") else url


def build_issue(ev: dict, ticks: int) -> tuple[str, str]:
    scores = ev["scores"]
    shape = "flat" if len(set(scores)) == 1 else "falling"
    title = f"{TITLE_PREFIX} {scores[-1]} {shape} for {ticks} ticks -- grading runs, nobody acts on it"
    body = "\n".join([
        "The fleet's self-improvement grade (self_improve_score.sh, every 3h) has not risen "
        f"across the last {ticks} ticks: " + " -> ".join(str(s) for s in scores) + ".",
        "",
        "That means the predictions ledger is not being closed: the grader is naming the same "
        "open rows and misses each tick and no member is resolving them. The loop that this "
        "score measures (predict -> build -> grade -> fix what the grade names) is stalled.",
        "",
        "What the grader said, oldest to newest:",
        *[f"- {r}" for r in ev["reasons"] if r],
        "",
        "Acceptance: the next self_improve_score.jsonl row is higher than "
        f"{scores[-1]}, or the ledger rows it names are resolved with evidence.",
        "",
        "<!-- rsi_stall_check -->",
        "Filed by scripts/rsi_stall_check.py (fk#1122), the mechanical replacement for the "
        "reading-the-grade duty that went unowned when dumbledore was deactivated.",
    ])
    return title, body


def open_titles(slug: str, run=_run) -> list[str]:
    r = run(["gh", "issue", "list", "--repo", slug, "--state", "open", "--limit", "300",
             "--json", "title", "--jq", ".[].title"])
    return [t for t in (r.stdout or "").splitlines() if t.strip()] if r.returncode == 0 else []


def check(log_dir: Path, ticks: int = DEFAULT_TICKS, run=_run, slug: str | None = None,
          dry_run: bool = False) -> str:
    """Returns one status word: no_data | rising | already_open | filed | dry_run | file_failed."""
    rows = read_scores(log_dir / "self_improve_score.jsonl")
    ev = stall(rows, ticks)
    if ev is None:
        return "no_data" if len(rows) < ticks else "rising"
    slug = slug or kit_slug()
    title, body = build_issue(ev, ticks)
    if any(t.strip().startswith(TITLE_PREFIX) for t in open_titles(slug, run)):
        return "already_open"
    if dry_run:
        print(title); print(body)
        return "dry_run"
    r = run(["gh", "issue", "create", "--repo", slug, "--title", title, "--label", LABELS, "--body", body])
    if r.returncode != 0:
        print(f"rsi_stall_check: could not file: {(r.stderr or r.stdout).strip()[:200]}", file=sys.stderr)
        return "file_failed"
    print(f"rsi_stall_check: filed {(r.stdout or '').strip().splitlines()[-1] if r.stdout else '(no url)'}")
    return "filed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log_dir = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
    status = check(log_dir, args.ticks, dry_run=args.dry_run)
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} rsi_stall_check {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
