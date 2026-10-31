#!/usr/bin/env python3
"""value_card -- the task's value card, for judge-judy (PHIA-1451).

Paul, 2026-10-31: the why and the research live on the task, not in the PR body. A seat writes a
value card on its task before code (User, Need, Data, Guess, Best in class, 4-risk self-score;
bug fixes and cuts get two lines), as the task document with key `value-card`. The judge reads
the card with the diff; a thin card is fixed by editing the card, and the judge re-reads it with
no new commit (judge-judy.sh compares the card's updatedAt with the block time).

Blind score: the self-score line is cut out of the card text the judge sees and handed back
separately as `plan`, so value_judge.py can log the gap after the judge has scored.

Usage: value_card.py TASK  (PHIA-123)  ->  JSON on stdout, `{}` when there is no board to ask.
  {"task", "owner", "card", "card_updated_at", "plan"}   card is "" when the task has none.
Reads PAPERCLIP_API_URL and PAPERCLIP_API_KEY (a read-only key) from the environment, and sends
PAPERCLIP_HOST as the Host header when set (PHIA-1504: lucky's worker reaches the board by IP). Never
fatal: any failure prints `{}` and the judge goes on without a card.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

CARD_KEY = os.environ.get("JUDGE_CARD_KEY", "value-card")
_SELF_RE = re.compile(r"^[^\n]*\b(self[- ]?score|4[- ]risk|four[- ]risk|plan)\b[^\n]*value\D{0,4}[0-3][^\n]*$",
                      re.IGNORECASE | re.MULTILINE)


def blind(card: str) -> tuple[str, str]:
    """(card without its self-score line, the self-score line or '')."""
    m = _SELF_RE.search(card or "")
    if not m:
        return card or "", ""
    return (card[:m.start()] + card[m.end():]).strip(), m.group(0)


def _get(path: str):
    base, key = os.environ.get("PAPERCLIP_API_URL"), os.environ.get("PAPERCLIP_API_KEY")
    if not base or not key:
        return None
    headers = {"Authorization": f"Bearer {key}"}
    if os.environ.get("PAPERCLIP_HOST"):  # the worker reaches the board by IP; it answers only to its own Host
        headers["Host"] = os.environ["PAPERCLIP_HOST"]
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception:
        return None


def fetch(task: str) -> dict:
    if not re.fullmatch(r"PHIA-\d+", task or ""):
        return {}
    issue = _get(f"/api/issues/{task}")
    if not isinstance(issue, dict):
        return {}
    owner = None
    if issue.get("assigneeAgentId"):
        agent = _get(f"/api/agents/{issue['assigneeAgentId']}")
        owner = (agent or {}).get("urlKey") or (agent or {}).get("name")
    doc = _get(f"/api/issues/{task}/documents/{CARD_KEY}")
    body = doc.get("body", "") if isinstance(doc, dict) else ""
    card, plan = blind(body)
    return {"task": task, "owner": owner, "card": card,
            "card_updated_at": doc.get("updatedAt") if isinstance(doc, dict) and body else None,
            "plan": plan}


if __name__ == "__main__":
    json.dump(fetch(sys.argv[1] if len(sys.argv) > 1 else ""), sys.stdout)
