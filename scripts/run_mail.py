#!/usr/bin/env python3
"""run_mail.py -- one email per finished run, written in plain English.

Reif, 2026-09-12: "I want all reports to be written in plain english" and "have those reports
sent to me each time something runs." persona_law.md §13 already tells every member to write in
freshman-101 language, and the reports still come back as file:line jargon (a judge-judy
verdict was the trigger). A prompt line that is ignored is not a mechanism. This is the
mechanism: run_report.py calls maybe_mail() on every completed run record; this module writes a
short plain-words summary of that record (a cheap model call, best effort), stacks the member's
own report under it, and hands the whole thing to messenger_brief.deliver() as kind "run".

Rules:
  - Only a run that actually did or failed something gets a mail. Liveness rows (started,
    heartbeat), idle ticks (quiet) and pacing holds (paced) are skipped -- ~half the rows in
    runs.jsonl are those, and none of them is "something ran".
  - Off unless FLEET_RUN_MAIL=1. Tests and fresh instances never mail by accident.
  - Never raises, never touches stdout, never changes the caller's exit code. The record on
    stdout IS the run row; a mail failure is a line in messenger.log, nothing more.
  - The plain-words paragraph comes from `claude -p` through the same account pool every
    member runs under (scripts/account_pool.sh), capped at a few cents. If that fails, the
    mail still goes with the member's own words -- late and plain beats never.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

KIT = pathlib.Path(__file__).resolve().parent.parent
SKIP_STATUSES = frozenset({"started", "heartbeat", "quiet", "paced"})
PLAIN_MODEL = os.environ.get("FLEET_RUN_MAIL_MODEL", "haiku")
PLAIN_BUDGET_USD = os.environ.get("FLEET_RUN_MAIL_BUDGET_USD", "0.05")
PLAIN_TIMEOUT_S = int(os.environ.get("FLEET_RUN_MAIL_TIMEOUT_S", "120"))

PLAIN_PROMPT = """Rewrite the fleet run below for a smart person who is not a programmer.

Rules: at most 120 words. Short sentences. Say what happened and what it means for the person
first, then anything they need to do. No file names, no function names, no code words, no
acronyms without their plain meaning. If the run failed or was cut short, say so plainly.
Answer with the rewrite only -- no heading, no preamble.

RUN:
"""


def enabled() -> bool:
    return os.environ.get("FLEET_RUN_MAIL", "").strip() == "1"


def wants_mail(rec: dict) -> bool:
    return bool(rec.get("member")) and rec.get("status") not in SKIP_STATUSES


def _links(rec: dict) -> list[str]:
    out = []
    repo = (os.environ.get("FLEET_REPO") or "").strip()
    base = f"https://github.com/{repo}" if repo else ""
    if rec.get("pr"):
        out.append(f"PR #{rec['pr']}" + (f": {base}/pull/{rec['pr']}" if base else ""))
    if rec.get("item_id"):
        out.append(f"item #{rec['item_id']}" + (f": {base}/issues/{rec['item_id']}" if base else ""))
    if rec.get("lane"):
        out.append(f"lane: {rec['lane']}")
    return out


def raw_text(rec: dict) -> str:
    """The member's own words, in the order a reader wants them."""
    parts = [f"Member: {rec.get('member')}", f"Status: {rec.get('status')}"]
    parts += _links(rec)
    if rec.get("outcome"):
        parts.append(f"Outcome: {rec['outcome']}")
    if rec.get("report"):
        parts.append(f"Report:\n{rec['report']}")
    if rec.get("evidence"):
        parts.append(f"Evidence: {rec['evidence']}")
    if rec.get("self_critique"):
        parts.append(f"Self-critique: {rec['self_critique']}")
    return "\n\n".join(parts)


def plain_words(rec: dict) -> str:
    """Best-effort plain-English rewrite. Empty string on any failure."""
    pool = KIT / "scripts" / "account_pool.sh"
    if not pool.exists():
        return ""
    script = (
        f'. "{pool}"; account_pool_run claude -p "$1" --model "$2" --output-format text '
        f'--max-budget-usd "$3" --dangerously-skip-permissions'
    )
    try:
        r = subprocess.run(["bash", "-c", script, "run_mail", PLAIN_PROMPT + raw_text(rec)[:6000],
                            PLAIN_MODEL, PLAIN_BUDGET_USD],
                           capture_output=True, text=True, timeout=PLAIN_TIMEOUT_S)
    except (subprocess.SubprocessError, OSError):
        return ""
    if r.returncode != 0:
        return ""
    text = (r.stdout or "").strip()
    return text if 0 < len(text) <= 1500 else ""


def compose(rec: dict, plain: str) -> str:
    outcome = (rec.get("outcome") or "").strip()
    head = f"# {rec.get('member')} · {rec.get('status')}" + (f" · {outcome[:80]}" if outcome else "")
    md = [head, ""]
    if plain:
        md += ["## In plain words", "", plain, ""]
    else:
        md += ["*(plain-words rewrite unavailable this run -- the member's own words follow)*", ""]
    for line in _links(rec):
        md.append(f"- {line}")
    if _links(rec):
        md.append("")
    if outcome:
        md += ["## What it did", "", outcome, ""]
    if rec.get("report"):
        md += ["## The member's report", "", rec["report"].strip(), ""]
    if rec.get("evidence"):
        md += ["## Evidence", "", rec["evidence"].strip(), ""]
    if rec.get("self_critique"):
        md += [f"Self-critique: {rec['self_critique'].strip()}", ""]
    tok = rec.get("tokens") or {}
    if tok.get("cost_usd") is not None or tok.get("num_turns") is not None:
        md.append(f"cost ${tok.get('cost_usd') or 0:.2f} · {tok.get('num_turns') or 0} turns · run {rec.get('run_id')}")
    return "\n".join(md).rstrip() + "\n"


def maybe_mail(rec: dict) -> str:
    """Returns the delivery result word ('sent', 'skipped', 'off', ...). Never raises."""
    try:
        if not enabled():
            return "off"
        if not wants_mail(rec):
            return "skipped"
        import messenger_brief as mb
        md = compose(rec, plain_words(rec))
        rc, word = mb.deliver("run", md, want_pdf=False, force=True)
        return word
    except Exception as exc:  # noqa: BLE001 -- side channel, never the run's exit code
        try:
            import messenger_brief as mb
            mb.log(f"run mail: {type(exc).__name__}: {exc} -- not sent")
        except Exception:  # noqa: BLE001
            pass
        return "error"


if __name__ == "__main__":
    import json
    print(maybe_mail(json.loads(sys.stdin.read())))
