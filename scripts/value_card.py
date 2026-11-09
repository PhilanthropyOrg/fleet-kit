#!/usr/bin/env python3
"""value_card -- the task's value card, for judge-judy (PHIA-1451).

Paul, 2026-10-31: the why and the research live on the task, not in the PR body. A seat writes a
value card on its task before code (User, Need, Data, Guess, Best in class, 4-risk self-score;
bug fixes and cuts get two lines), as the task document with key `value-card`. The judge reads
the card with the diff; a thin card is fixed by editing the card, and the judge re-reads it with
no new commit (judge-judy.sh compares the card's updatedAt with the block time).

Blind score: the self-score line is cut out of the card text the judge sees and handed back
separately as `plan`, so value_judge.py can log the gap after the judge has scored.

Usage: value_card.py TASK  (PHIA-123)  ->  JSON on stdout, `{}` when TASK is not a task id.
  {"task", "owner", "card", "card_updated_at", "yes_at", "plan", "task_text", "labels", "created_at", "filed_by"}   card is "" when the task has none.
  yes_at is when the task got its `judge-yes` or `fast-lane` label, or null: that YES changes no commit, no body and no card, so
  judge-judy.sh compares it with the block time too (11-06, PR 15768 kept its block after the YES).
  filed_by is "owner" when a person filed the task on the board, "seat" when an agent did, "" when unknown
  (PHIA-2780: the anchors price an Owner-filed task and a `judge-yes` or `fast-lane` label at value 2, so the
  judge must be told which the task is; without it, staff and server PRs sat at value 1 "like 14824").
  task_text is the task's title and description (self-score cut out): Paul, 2026-11-01, "don't wait
  for cards" -- with no card the judge treats the task description plus the PR's Why as the card.
  {"task", "error"}   when the board could not be read, so the score table says why, not "no card".
Reads PAPERCLIP_API_URL and PAPERCLIP_API_KEY (a read-only key) from the environment, and sends
PAPERCLIP_HOST as the Host header when set (PHIA-1504: lucky's worker reaches the board by IP). Never
fatal: any failure prints `{}` and the judge goes on without a card.
Each read waits JUDGE_CARD_TIMEOUT seconds (default 150): board reads ran p95 ~105 s on 10-31 (PHIA-1340)
and 7 of 13 verdicts said "card unread: TimeoutError" at 20 s. The issue and the card are read at once.
Paul, 11-01 06:11Z: 5 of 8 verdicts still said "card unread: TimeoutError" at 150 s (board under load,
PHIA-1929). So a read that fails is tried once more (JUDGE_CARD_TRIES, default 2), and every good read is
kept in JUDGE_CARD_CACHE (a folder; judge-judy.sh sets it): when both tries fail, the last good read of
that task is used and the table says "card, saved <time>", so a slow board no longer costs the card.
PHIA-2276: the first cut of that retry let one card read run 600 s (two tries of 150 s, then the agent
name read the same way after them), and the app reviewer went silent for an hour after it merged. Now the
whole read has one budget, JUDGE_CARD_BUDGET seconds (default JUDGE_CARD_TIMEOUT): each try waits only for
what is left of it, a retry starts only with 5 s or more left, and the agent name is skipped once it is spent.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

CARD_KEY = os.environ.get("JUDGE_CARD_KEY", "value-card")
TASK_TEXT_BYTES = int(os.environ.get("JUDGE_TASK_TEXT_BYTES") or 3000)
TIMEOUT = float(os.environ.get("JUDGE_CARD_TIMEOUT") or 150)
TRIES = max(1, int(os.environ.get("JUDGE_CARD_TRIES") or 2))
BUDGET = float(os.environ.get("JUDGE_CARD_BUDGET") or TIMEOUT)
YES_LABELS = {"judge-yes", "fast-lane", "hack-solo"}  # hack-solo (Reif 11-09): work he runs directly; see value_judge.HACK_SOLO
MIN_TRY = 5.0  # seconds: a retry with less than this left is not worth starting
_deadline = [float("inf")]
_SELF_RE = re.compile(r"^[^\n]*\b(self[- ]?score|4[- ]risk|four[- ]risk|plan)\b[^\n]*value\D{0,4}[0-3][^\n]*$",
                      re.IGNORECASE | re.MULTILINE)


def blind(card: str) -> tuple[str, str]:
    """(card without its self-score line, the self-score line or '')."""
    m = _SELF_RE.search(card or "")
    if not m:
        return card or "", ""
    return (card[:m.start()] + card[m.end():]).strip(), m.group(0)


def _get(path: str):
    """(json or None, why it is None: '' on success), tried TRIES times; a 4xx answer is not retried."""
    why = "card read budget spent"
    for n in range(TRIES):
        left = _deadline[0] - time.monotonic()
        if left <= 0 or (n and left < MIN_TRY):
            break
        data, why = _get_once(path, min(TIMEOUT, left))
        if not why or why.startswith("HTTP 4") or "not in the judge's environment" in why:
            return data, why
    return None, why


def _cache_path(name: str) -> str | None:
    folder = os.environ.get("JUDGE_CARD_CACHE")
    return os.path.join(folder, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json") if folder else None


def _cache_put(name: str, value: dict) -> None:
    path = _cache_path(name)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(dict(value, cached_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")), f)
        os.replace(tmp, path)
    except OSError:
        pass


def _cache_get(name: str) -> dict | None:
    path = _cache_path(name)
    try:
        with open(path) as f:  # type: ignore[arg-type]
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def _get_once(path: str, timeout: float = TIMEOUT):
    """(json or None, why it is None: '' on success) -- the why goes to the score table (PHIA-1504)."""
    base, key = os.environ.get("PAPERCLIP_API_URL"), os.environ.get("PAPERCLIP_API_KEY")
    if not base or not key:
        return None, "PAPERCLIP_API_URL or PAPERCLIP_API_KEY not in the judge's environment"
    headers = {"Authorization": f"Bearer {key}"}
    if os.environ.get("PAPERCLIP_HOST"):  # the worker reaches the board by IP; it answers only to its own Host
        headers["Host"] = os.environ["PAPERCLIP_HOST"]
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # never fatal: the judge goes on without a card
        return None, type(e).__name__ + (f": {getattr(e, 'reason', '')}" if getattr(e, "reason", "") else "")


def yes_at(labels: list, rows: list) -> str | None:
    """When the task's Judge YES (or fast-lane) label last went on; None when it has none."""
    ids = {l.get("id") for l in labels if isinstance(l, dict) and l.get("name") in YES_LABELS}
    times = []
    for row in rows if ids else []:
        change = ((row.get("details") or {}).get("changes") or {}).get("labelIds") or {}
        if ids & (set(change.get("to") or []) - set(change.get("from") or [])):
            times.append(row.get("createdAt") or "")
    return max(times, default="") or None


def fetch(task: str) -> dict:
    if not re.fullmatch(r"PHIA-\d+", task or ""):
        return {}
    _deadline[0] = time.monotonic() + BUDGET
    out = _fetch(task)
    if "error" not in out:
        _cache_put(task, out)
        return out
    old = _cache_get(task)  # the board failed twice: the last good read beats no card at all
    if old:
        return dict(old, read_failed=out["error"])  # table: "card, saved read" -- not "card unread"
    return out


def _fetch(task: str) -> dict:
    with ThreadPoolExecutor(2) as pool:  # slow board: read the issue and the card at once
        card_read = pool.submit(_get, f"/api/issues/{task}/documents/{CARD_KEY}")
        issue, issue_error = _get(f"/api/issues/{task}")
        if not isinstance(issue, dict):
            return {"task": task, "error": issue_error or "board answered with no issue"}
        owner = None
        if issue.get("assigneeAgentId"):  # an agent's name never changes: read it once
            aid = issue["assigneeAgentId"]
            owner = (_cache_get(f"agent-{aid}") or {}).get("owner")
            if not owner and time.monotonic() < _deadline[0]:
                agent, _ = _get(f"/api/agents/{aid}")
                owner = (agent or {}).get("urlKey") or (agent or {}).get("name")
                if owner:
                    _cache_put(f"agent-{aid}", {"owner": owner})
        doc, doc_error = card_read.result()
    doc_error = "" if isinstance(doc, dict) or doc_error == "HTTP 404" else doc_error
    body = doc.get("body", "") if isinstance(doc, dict) else ""
    card, plan = blind(body)
    task_text, task_plan = blind(f"{issue.get('title') or ''}\n{issue.get('description') or ''}".strip())
    # Reif 11-02: the Judge approves a pitch by labelling its task `judge-yes` (or the seat marks a breakage
    # `fast-lane`); value_judge.py reads the label names and the task's age.
    labels = [l.get("name") if isinstance(l, dict) else l for l in (issue.get("labels") or [])]
    rows, _ = _get(f"/api/issues/{task}/activity") if YES_LABELS & set(labels) else (None, "")
    out = {"task": task, "owner": owner, "card": card,
           "card_updated_at": doc.get("updatedAt") if isinstance(doc, dict) and body else None,
           "yes_at": yes_at(issue.get("labels") or [], rows if isinstance(rows, list) else []),
           "plan": plan or task_plan, "task_text": task_text[:TASK_TEXT_BYTES],
           "labels": [l for l in labels if l], "created_at": issue.get("createdAt"),
           "filed_by": "owner" if issue.get("createdByUserId") else ("seat" if issue.get("createdByAgentId") else "")}
    if doc_error:  # a 404 is a task with no card; anything else is a read that failed
        out["error"] = doc_error
    return out


if __name__ == "__main__":
    json.dump(fetch(sys.argv[1] if len(sys.argv) > 1 else ""), sys.stdout)
