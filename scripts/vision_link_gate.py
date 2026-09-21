#!/usr/bin/env python3
"""vision_link_gate -- is this candidate eligible for gru to pick, gh#525?

THE GAP THIS CLOSES. gh#523 named the failure directly: gru picks purely from marie's
`fleet:priority-<tier>` labels by `createdAt` (gru.md step 2b) -- whether a candidate's own
PRD says it moves the number introduced by #513 ("Vision-link:") is not read at all. Result:
12 same-morning minion PRs, all `fix(...)` inward-spend, none evaluated against whether they
move the number.

THE RULE (fk#1191, Reif 2026-09-21: "making it blind to any portion of the backlog is
insane"). A candidate is eligible if its body/PRD carries a `Vision-link:` line -- either a
registered KR id or an explicit `none (maintenance)`. A candidate with no line at all is never
eligible on its own. That is the whole gate. The crowd-out rule this used to carry (gh#525:
maintenance ineligible while ANY linked candidate is open) is gone: measured 2026-09-21 it hid
117 of 184 unclaimed items behind ONE linked ticket (#7020) that itself failed quality_gate, so
gru reported "0 buildable" for three passes on a 229-item board. KR-first is marie's RANKING
job (priority tiers), not an eligibility rule; a gate that empties the board is not a gate.
The gh#726 severity hatch is kept only as a label passthrough (nothing to escape any more).

WHERE THE LINE LIVES. A `Vision-link:` line can live directly in an issue BODY (Reif filing an
item names its link himself), in a `fleet:prd` PRD comment (marie scoring it), or in a
lightweight non-PRD comment (marie's gh#4597 backfill) -- this script reads every comment the
same way regardless of what kind it is or what label the issue carries; it has no concept of
`fleet:prd` at all. Same "latest wins" rule gru.md and marie.md already use elsewhere for PRD
comments superseding an earlier one: the newest comment carrying the line wins over an older
comment, which wins over the body. Reuses run_report._vision_claim's regex (tolerates markdown
heading/bold wrapping) rather than a second parser for the same field.

Pure core (`classify_candidate`/`gate_candidates`), thin CLI (`main`) -- same split as
claim_history.py and cost_bridge.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_report  # noqa: E402

STATUS_LINKED = "linked"
STATUS_MAINTENANCE = "maintenance"
STATUS_MISSING = "missing"

# gh#726's crowd-out escape hatch. Kept as a public name (other scripts import it); the
# crowd-out it escaped from was removed in fk#1191, so it no longer changes eligibility.
SEVERITY_LIVE_LABEL = "fleet:severity-live"

# Tolerate the punctuation a model actually produces: "none(maintenance)", "None (Maintenance)",
# and a trailing qualifier with no dash separator at all ("none (fleet guardrail/maintenance).",
# gh#584) -- matched as a prefix on the normalized (lowercased, whitespace-stripped) value, not
# an exact-match on a dash-delimited head, so a reason clause after the parenthetical (dashed or
# not) never defeats the match, and a real link that merely mentions "maintenance" (not starting
# with "none(") is never swallowed by it.
_MAINTENANCE_RE = re.compile(r"^none\(.*maintenance.*?\)")


def _normalize(value: str) -> str:
    return "".join(value.lower().split())


def classify_candidate(body: str | None, comments: list[dict] | None) -> tuple[str, str | None]:
    """(status, raw Vision-link value) for one candidate.

    `comments` should be in `createdAt` order (ascending), same shape `gh issue list --json
    number,...,comments` returns. Newest comment carrying a `Vision-link:` line wins over an
    older one, which wins over the body -- a re-scored PRD supersedes what it superseded.
    """
    for comment in reversed(comments or []):
        claim = run_report._vision_claim(comment.get("body") or "")
        if claim is not None:
            return _classify_value(claim)
    claim = run_report._vision_claim(body or "")
    if claim is not None:
        return _classify_value(claim)
    return STATUS_MISSING, None


def kr_ids(path: Path | None = None) -> list[str]:
    """The registered OKR ids (scripts/okr.json, or FLEET_OKR_FILE): the objective plus every
    key result. Empty when the file is unreadable -- and then nothing can be linked, which is
    loud on purpose."""
    import os
    p = path or Path(os.environ.get("FLEET_OKR_FILE") or (HERE / "okr.json"))
    try:
        d = json.loads(p.read_text())
        return [d["objective"]["id"]] + [k["id"] for k in d.get("key_results", [])]
    except Exception:  # noqa: BLE001
        return []


def _classify_value(raw: str) -> tuple[str, str]:
    # A maintenance value is only ever "none (...maintenance...)" -- a prefix match on the
    # normalized value, so a reason clause never defeats it (gh#584).
    if _MAINTENANCE_RE.match(_normalize(raw)):
        return STATUS_MAINTENANCE, raw
    # Reif, 2026-09-16, on six merged PRs none of which moved a KR: "shouldnt that be the case
    # with setting up the okrs, we did this". It was not, because any prose counted as a link
    # -- live: "KR2 -- sixteen members opened a thread" passed while VISION.md has no messaging
    # KR. A link now names a registered id (scripts/okr.json); prose without one is MISSING,
    # which marie's restamp sweep repairs and gru cannot build past.
    low = raw.lower()
    if any(k in low for k in kr_ids()):
        return STATUS_LINKED, raw
    return STATUS_MISSING, raw


def gate_candidates(candidates: list[dict]) -> dict:
    """candidates: [{"number": int, "body": str, "comments": [...]}, ...], already in the
    order gru.md step 2b/2c produced (tier, then oldest-createdAt-first within a tier).
    `labels` is optional (gh#726) -- a candidate dict with no `labels` key at all behaves
    exactly as it did before this key existed: no escape hatch, byte-identical output.

    Returns {"eligible": [numbers, in the same relative order], "dropped": [{"number",
    "reason"}, ...]} -- gh#525 AC3: every drop is named, never a silent absence.
    """
    classified = [
        (c["number"], *classify_candidate(c.get("body"), c.get("comments")), c.get("labels"))
        for c in candidates
    ]
    eligible: list[int] = []
    dropped: list[dict] = []
    for number, status, raw, labels in classified:
        if status in (STATUS_LINKED, STATUS_MAINTENANCE):
            eligible.append(number)
        else:
            dropped.append({
                "number": number,
                "reason": "no Vision-link line (neither a real link nor explicit "
                          "'none (maintenance)')",
            })
    return {"eligible": eligible, "dropped": dropped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Gate marie-ranked candidates on gh#525's Vision-link eligibility rule. "
                     "Reads --items JSON (same candidates gru.md step 2b/2c already pulled, "
                     "each with number/body/comments), writes {eligible, dropped} to stdout.")
    ap.add_argument("--items", required=True,
                     help="JSON list: [{\"number\":n,\"body\":\"...\",\"comments\":[...]}, ...]")
    a = ap.parse_args(argv)

    candidates = json.loads(a.items)
    print(json.dumps(gate_candidates(candidates)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
