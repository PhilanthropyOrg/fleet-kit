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

PLAIN_PROMPT = """Rewrite this whole fleet run for a smart person who is not a programmer.
Reif reads these on his phone. He is not going to translate jargon.

Write it as:
A first line: what happened, in plain words.
Then "What this means:" one or two sentences on why it matters to him.
Then "What to do:" the decision or action he has to take, or "Nothing -- this is just a receipt."

Rules: at most 160 words total. Short sentences, common words. Never use a file name, a
function name, a branch name, a command, or a code word. Never use an acronym without saying
what it means. Say "the issue about X" rather than a bare number where you can. If the run
failed, was cut short, or ran out of money, say that first and plainly. Do not invent anything
that is not in the run below. Answer with the rewrite only -- no heading, no preamble.

RUN:
"""


def enabled() -> bool:
    return os.environ.get("FLEET_RUN_MAIL", "").strip() == "1"


def wants_mail(rec: dict) -> bool:
    return bool(rec.get("member")) and rec.get("status") not in SKIP_STATUSES


def repo_slug() -> str:
    """owner/name for building links, or "" when we cannot know it.

    FLEET_REPO is the checkout PATH inside the container ("/repo"), never a slug -- building
    a URL from it produced `https://github.com//repo/issues/4996` in the first real run mail
    (Reif, 2026-09-12). FLEET_REPO_URL is the git remote and is the only reliable source; a
    fleet.env may set it more than once (one per instance), and the last assignment is the
    one the shell exported, which is what os.environ already reflects. No slug -> no link,
    never a broken one.
    """
    url = (os.environ.get("FLEET_REPO_URL") or "").strip()
    if url:
        slug = url.rsplit("github.com", 1)[-1].lstrip(":/").removesuffix(".git").strip("/")
        if slug.count("/") == 1 and all(slug.split("/")):
            return slug
    repo = (os.environ.get("FLEET_REPO") or "").strip().strip("/")
    # Only if it already looks like owner/name -- never a filesystem path.
    if repo.count("/") == 1 and not repo.startswith(".") and all(repo.split("/")):
        return repo
    return ""


def _links(rec: dict) -> list[str]:
    """Plain-language pointers. A link only when we have a real slug to build it from."""
    out = []
    slug = repo_slug()
    base = f"https://github.com/{slug}" if slug else ""
    if rec.get("pr"):
        out.append(f"The change: #{rec['pr']}" + (f" -- {base}/pull/{rec['pr']}" if base else ""))
    if rec.get("item_id"):
        out.append(f"The item of work: #{rec['item_id']}" + (f" -- {base}/issues/{rec['item_id']}" if base else ""))
    if rec.get("lane"):
        out.append(f"Area of work: {rec['lane']}")
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
        f'--max-budget-usd "$3"'
    )
    # No --dangerously-skip-permissions: the container runs as root, where the CLI refuses that
    # flag, and a text-only rewrite never calls a tool, so it never needs one approved.
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
    """The email. Plain English is the WHOLE body, not a summary bolted on top of jargon.

    Reif, 2026-09-12, on the first real run mail: "reports in plain english please" -- the
    top section read plainly and everything under it was raw member output. The member's own
    words still travel, but as a clearly-labelled appendix a reader can ignore: deleting the
    evidence outright would break the report contract's "every claim traces to something that
    was actually run" (persona_law.md 10c).
    """
    status_plain = {
        "ok": "finished",
        "quiet": "found nothing to do",
        "reported_nothing": "finished but did not say what it did",
        "killed": "was interrupted", "timed_out": "ran out of time",
        "budget_declined": "stopped to stay inside its budget", "paced": "was held back to save budget",
        "no_vision_link": "finished but did not say which goal it moved",
        "incomplete_fanout": "handed work out but never reported back",
        "report_lost": "finished but its report was lost",
    }.get(rec.get("status"), rec.get("status") or "finished")

    md = [f"# {rec.get('member')} {status_plain}", ""]
    if plain:
        md += [plain.strip(), ""]
    else:
        md += ["This run could not be rewritten in plain words -- the helper's own report is below.", ""]
    links = _links(rec)
    if links:
        md += ["## Where to look", ""] + [f"- {line}" for line in links] + [""]
    md += ["---", "",
           "<small>The rest is the helper's own words, kept so every claim above can be checked.</small>", ""]
    if (rec.get("outcome") or "").strip():
        md += ["**What it reported:** " + rec["outcome"].strip(), ""]
    if rec.get("report"):
        md += [rec["report"].strip(), ""]
    if rec.get("evidence"):
        md += ["**How it checked:** " + rec["evidence"].strip(), ""]
    if rec.get("self_critique"):
        md += ["**What it thinks it got wrong:** " + rec["self_critique"].strip(), ""]
    tok = rec.get("tokens") or {}
    if tok.get("cost_usd") is not None or tok.get("num_turns") is not None:
        md.append(f"<small>cost ${tok.get('cost_usd') or 0:.2f} · {tok.get('num_turns') or 0} turns · run {rec.get('run_id')}</small>")
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
