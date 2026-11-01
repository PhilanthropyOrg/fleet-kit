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
  {"task", "owner", "card", "card_updated_at", "plan", "task_text"}   card is "" when the task has none.
  task_text is the task's title and description (self-score cut out): Paul, 2026-11-01, "don't wait
  for cards" -- with no card the judge treats the task description plus the PR's Why as the card.
  {"task", "error"}   when the board could not be read, so the score table says why, not "no card".
Reads PAPERCLIP_API_URL and PAPERCLIP_API_KEY (a read-only key) from the environment, and sends
PAPERCLIP_HOST as the Host header when set (PHIA-1504: lucky's worker reaches the board by IP). Never
fatal: any failure prints `{}` and the judge goes on without a card.
Each read waits JUDGE_CARD_TIMEOUT seconds (default 150): board reads ran p95 ~105 s on 10-31 (PHIA-1340)
and 7 of 13 verdicts said "card unread: TimeoutError" at 20 s. The issue and the card are read at once.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CARD_KEY = os.environ.get("JUDGE_CARD_KEY", "value-card")
TASK_TEXT_BYTES = int(os.environ.get("JUDGE_TASK_TEXT_BYTES") or 3000)
TIMEOUT = float(os.environ.get("JUDGE_CARD_TIMEOUT") or 150)
_SELF_RE = re.compile(r"^[^\n]*\b(self[- ]?score|4[- ]risk|four[- ]risk|plan)\b[^\n]*value\D{0,4}[0-3][^\n]*$",
                      re.IGNORECASE | re.MULTILINE)


def blind(card: str) -> tuple[str, str]:
    """(card without its self-score line, the self-score line or '')."""
    m = _SELF_RE.search(card or "")
    if not m:
        return card or "", ""
    return (card[:m.start()] + card[m.end():]).strip(), m.group(0)


def _get(path: str):
    """(json or None, why it is None: '' on success) -- the why goes to the score table (PHIA-1504)."""
    base, key = os.environ.get("PAPERCLIP_API_URL"), os.environ.get("PAPERCLIP_API_KEY")
    if not base or not key:
        return None, "PAPERCLIP_API_URL or PAPERCLIP_API_KEY not in the judge's environment"
    headers = {"Authorization": f"Bearer {key}"}
    if os.environ.get("PAPERCLIP_HOST"):  # the worker reaches the board by IP; it answers only to its own Host
        headers["Host"] = os.environ["PAPERCLIP_HOST"]
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # never fatal: the judge goes on without a card
        return None, type(e).__name__ + (f": {getattr(e, 'reason', '')}" if getattr(e, "reason", "") else "")


def fetch(task: str) -> dict:
    if not re.fullmatch(r"PHIA-\d+", task or ""):
        return {}
    with ThreadPoolExecutor(2) as pool:  # slow board: read the issue and the card at once
        card_read = pool.submit(_get, f"/api/issues/{task}/documents/{CARD_KEY}")
        issue, issue_error = _get(f"/api/issues/{task}")
        if not isinstance(issue, dict):
            return {"task": task, "error": issue_error or "board answered with no issue"}
        owner = None
        if issue.get("assigneeAgentId"):
            agent, _ = _get(f"/api/agents/{issue['assigneeAgentId']}")
            owner = (agent or {}).get("urlKey") or (agent or {}).get("name")
        doc, doc_error = card_read.result()
    doc_error = "" if isinstance(doc, dict) or doc_error == "HTTP 404" else doc_error
    body = doc.get("body", "") if isinstance(doc, dict) else ""
    card, plan = blind(body)
    task_text, task_plan = blind(f"{issue.get('title') or ''}\n{issue.get('description') or ''}".strip())
    out = {"task": task, "owner": owner, "card": card,
           "card_updated_at": doc.get("updatedAt") if isinstance(doc, dict) and body else None,
           "plan": plan or task_plan, "task_text": task_text[:TASK_TEXT_BYTES]}
    if doc_error:  # a 404 is a task with no card; anything else is a read that failed
        out["error"] = doc_error
    return out


if __name__ == "__main__":
    json.dump(fetch(sys.argv[1] if len(sys.argv) > 1 else ""), sys.stdout)
