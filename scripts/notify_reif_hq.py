#!/usr/bin/env python3
"""notify_reif_hq.py -- tail logs/merged-prs.jsonl, tell Reif HQ about every merged PR.

Reif: his always-on chat 'Reif HQ' (dino, tmux session 'reif', Claude Code + Remote Control,
rules in ~/CLAUDE.md and ~/reif-chat/MANIFESTO.md "Done means live") must hear about every PR
merged to PhilanthropyOrg/philanthropy's default branch -- pushed, not polled. The push side is
webhook_receiver.py's _handle_pull_request(), which appends one line to merged-prs.jsonl inside
the container (bind-mounted to the host at <instance>/logs/merged-prs.jsonl). This script is the
HOST side: a systemd .path unit fires it on every write to that file; it reads whatever's new
since its own offset, batches merges that land within BATCH_WINDOW_S of each other into one tmux
message, and sends it to the 'reif' pane with `tmux send-keys`.

Offset is tracked in a sibling `.offset` file next to the jsonl -- a .path unit can fire more
than once per burst of writes, and re-reading from byte 0 every time would re-inject every PR
ever merged on every tick.

SANITIZE: a PR title is attacker-controlled text (anyone who can open/merge a PR into the repo
picks it) landing in a shell command via tmux send-keys -- strip control chars and backticks,
clip to 120 chars, so a title can't break out of the message or smuggle a shell metachar into
the pane's input line.

Usage: notify_reif_hq.py <merged-prs.jsonl path> [--tmux-session reif]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

BATCH_WINDOW_S = 60
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_title(title: str) -> str:
    """Strip control chars and backticks (command substitution inside the pane's shell), clip
    to 120 chars -- a merged PR's title must never be able to inject a command into Reif HQ's
    input line, only ever appear as inert text inside the message."""
    cleaned = CONTROL_CHARS_RE.sub("", title or "").replace("`", "'")
    cleaned = " ".join(cleaned.split())  # collapse newlines/tabs that survived as whitespace
    if len(cleaned) > 120:
        cleaned = cleaned[:117] + "..."
    return cleaned


def format_message(records: list[dict]) -> str:
    """One line per PR if batched, one shared instruction -- same 'Done means live' contract
    whether it's one merge or five landing inside the same 60s window."""
    lines = [
        f"PR #{r.get('number')} merged: {sanitize_title(r.get('title', ''))} {r.get('url', '')}"
        for r in records
    ]
    body = "\n".join(lines)
    return (
        f"{body}\n"
        "Per Done-means-live: if this closes one of Reif's asks, verify it on prod and "
        "report; otherwise ignore."
    )


def read_new_records(jsonl_path: Path, offset_path: Path) -> tuple[list[dict], int]:
    """Returns (records, end_offset). Caller advances the offset file only after each batch is
    actually SENT -- a batch skipped because HQ looked mid-turn must be re-read (not lost) on
    the .path unit's next fire, so the offset write is the sender's job, not this reader's."""
    start = 0
    if offset_path.exists():
        try:
            start = int(offset_path.read_text().strip() or 0)
        except ValueError:
            start = 0
    if not jsonl_path.exists():
        return [], start
    size = jsonl_path.stat().st_size
    if start > size:  # file was rotated/truncated -- don't crash, don't replay the old world
        start = 0
    records = []
    with jsonl_path.open("rb") as fh:
        fh.seek(start)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        end = fh.tell()
    return records, end


def batch_by_window(records: list[dict], window_s: int = BATCH_WINDOW_S) -> list[list[dict]]:
    """Groups records whose merged_at timestamps land within window_s of the first record in
    the group -- a fast-forward merge queue landing 3 PRs in 20s becomes one message, not three
    separate interruptions to Reif HQ's turn."""
    def parse_ts(r: dict) -> float:
        raw = r.get("merged_at") or ""
        try:
            return time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return time.time()

    batches: list[list[dict]] = []
    for r in sorted(records, key=parse_ts):
        if batches and parse_ts(r) - parse_ts(batches[-1][0]) <= window_s:
            batches[-1].append(r)
        else:
            batches.append([r])
    return batches


def hq_mid_turn(tmux_session: str) -> bool:
    """Best-effort: if the pane's last line doesn't look like a shell/REPL prompt waiting for
    input, assume Claude Code is still mid-turn and skip injecting this tick -- the .path unit
    fires again on the next write, or the caller falls back to queueing (Claude Code queues
    input typed mid-turn, so a wrong guess here is a delay, never a lost message)."""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-pt", tmux_session, "-S", "-5"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False  # can't tell -- don't block delivery on a detection failure
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    busy_markers = ("esc to interrupt", "…", "Thinking", "tokens")
    return any(m in tail for m in busy_markers)


def send_to_hq(message: str, tmux_session: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", tmux_session, message], check=True)
    subprocess.run(["tmux", "send-keys", "-t", tmux_session, "Enter"], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl_path")
    ap.add_argument("--tmux-session", default="reif")
    args = ap.parse_args()

    jsonl_path = Path(args.jsonl_path)
    offset_path = jsonl_path.with_suffix(jsonl_path.suffix + ".offset")

    records, end_offset = read_new_records(jsonl_path, offset_path)
    if not records:
        return 0

    # One gate for the whole run, checked BEFORE any send: a batch is never partially sent, so
    # there is no "advance the offset past what we already sent" bookkeeping to get wrong. If
    # HQ looks mid-turn, leave the offset file untouched -- the .path unit fires again on the
    # NEXT merge (or a later manual run), and this run's records get re-read and re-batched
    # then, unsent.
    if hq_mid_turn(args.tmux_session):
        print(f"reif HQ looks mid-turn, deferring {len(records)} merge(s) to next fire", file=sys.stderr)
        return 0

    for batch in batch_by_window(records):
        send_to_hq(format_message(batch), args.tmux_session)
    offset_path.write_text(str(end_offset))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
