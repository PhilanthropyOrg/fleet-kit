#!/usr/bin/env python3
"""judge_judy_verdict -- read judge-judy's review verdict from validated JSON, not a regex
scraped over free prose (gh#806).

`claude -p --output-format json --json-schema <schema>` enforces the verdict schema at the
tool-call layer: the model's answer is forced through a schema-conforming tool call, retried
internally by the CLI before this script ever sees the output (gh#806 PRD AC3 -- a schema
mismatch on the model's first internal attempt never reaches, and never increments, judge-judy.sh's
own strike counter; a strike there means the CLI itself gave up after its own retries).

Confirmed live against the installed CLI 2026-09-11 (resolves the PRD's own UNKNOWN): the
envelope carries the validated object TWICE -- already parsed as `structured_output`, and
json-encoded a second time inside the string field `result` (the field pass_accounting.py's
`text` subcommand reads, kept for callers that only look there). Prefer `structured_output`;
fall back to a second `json.loads` of `result` for a CLI build that only sets the string form.

Usage: `claude -p ... --output-format json --json-schema "$SCHEMA" | python3 judge_judy_verdict.py`
Exit 0, `{"ok": true, "verdict": "approve"|"block", "findings": [...], "findings_text": "..."}`
  on stdout, when the object validates.
`--need-metric` (PHIA-2915): a verdict that is not a cut must name a daily metric (vertical/metric) and a guess
  that is not 0, or it is refused with a reason that starts "no metric"; judge-judy.sh asks once more, then takes
  the second answer as it is.
Exit 1, `{"ok": false, "reason": "<what was wrong>"}` on stdout on any failure -- the outer
  envelope wasn't JSON, neither structured_output nor a parseable result was present, or the
  object failed the verdict's own shape checks. judge-judy.sh treats exit 1 as a schema-parse
  strike (gh#221) and holds the PR after MAX_PARSE_STRIKES, never leaves it unjudged (AC2).
"""
from __future__ import annotations

import json
import math
import sys

VALID_VERDICTS = ("approve", "block")
# "plain": Reif, 2026-09-12 -- every finding carries one or two sentences a smart person
# outside software can read, next to the technical what_breaks. persona_law.md §13.
REQUIRED_FINDING_KEYS = ("file", "line", "severity", "what_breaks", "plain")
NO_METRIC = "no metric"  # judge-judy.sh keys the one re-ask on this reason prefix


def metric_missing(beneficiary) -> bool:
    """PHIA-2915: a PR that is not a cut names the daily metric it should move and a guess, so the 48-hour
    read can judge it. A cut keeps metric none; a verdict with no beneficiary block (older schema) is not held."""
    if not isinstance(beneficiary, dict) or beneficiary.get("cut") is True:
        return False
    metric = str(beneficiary.get("metric") or "").strip()
    guess = beneficiary.get("guess")
    ok_guess = isinstance(guess, (int, float)) and not isinstance(guess, bool) and math.isfinite(guess) and guess != 0
    return "/" not in metric or not ok_guess


def _findings_text(findings: list) -> str:
    lines = []
    for f in findings:
        lines.append(f"- **{f.get('severity', '?')}** -- {f.get('plain', '?')}")
        lines.append(f"  Where: {f.get('file', '?')}:{f.get('line', '?')}. Technical: {f.get('what_breaks', '?')}")
    return "\n".join(lines)


def _last_envelope(raw: str):
    """The account pool retries on a second account when the first answers 429, and both
    calls print to the same stdout -- so RAW can hold two envelopes, one per line: the
    rate-limit error, then the real answer. A single json.loads failed on that with "Extra
    data: line 2 column 1" (18 of 21 schema strikes in judge-judy.log, 2026-10-03; PR #10685
    and #10706 were held for it). The last envelope is the one that answered."""
    dec, i, last = json.JSONDecoder(), 0, None
    raw = raw.strip()
    while i < len(raw):
        last, i = dec.raw_decode(raw, i)
        while i < len(raw) and raw[i].isspace():
            i += 1
    return last


def parse(raw: str, need_metric: bool = False) -> dict:
    try:
        envelope = _last_envelope(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": f"outer --output-format json envelope did not parse: {e}"}
    if not isinstance(envelope, dict):
        return {"ok": False, "reason": f"outer envelope was not a JSON object (got {type(envelope).__name__})"}

    obj = envelope.get("structured_output")
    if obj is None:
        result = envelope.get("result")
        if not isinstance(result, str):
            return {"ok": False, "reason": "no structured_output and no string result field in envelope"}
        try:
            obj = json.loads(result)
        except json.JSONDecodeError as e:
            return {"ok": False, "reason": f"result field was not valid JSON: {e}"}

    if not isinstance(obj, dict):
        return {"ok": False, "reason": f"verdict object was not a JSON object (got {type(obj).__name__})"}

    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        return {"ok": False, "reason": f"verdict field missing or not one of {VALID_VERDICTS} (got {verdict!r})"}

    findings = obj.get("findings")
    if not isinstance(findings, list):
        return {"ok": False, "reason": f"findings field missing or not a list (got {type(findings).__name__ if findings is not None else 'missing'})"}
    for i, f in enumerate(findings):
        if not isinstance(f, dict) or not all(k in f for k in REQUIRED_FINDING_KEYS):
            return {"ok": False, "reason": f"findings[{i}] missing a required key ({'/'.join(REQUIRED_FINDING_KEYS)})"}

    # A block with no findings is as useless as no verdict at all -- it posts a hard,
    # required-check-blocking failure with nothing a human or the-fixer can act on (issue
    # #3170). Fold this into the same schema-violation path rather than a second check
    # downstream: judge-judy.sh only ever needs one "was this a valid answer" gate.
    if verdict == "block" and not findings:
        return {"ok": False, "reason": "verdict=block with an empty findings list -- nothing a human can act on"}

    if need_metric and metric_missing(obj.get("beneficiary")):
        return {"ok": False, "reason": f"{NO_METRIC} or guess on a PR that is not a cut: name one daily metric "
                                       "(vertical/metric) and a guess that is not 0, or set cut true (PHIA-2915)"}

    out = {"ok": True, "verdict": verdict, "findings": findings, "findings_text": _findings_text(findings)}
    # PHIA-1451: the four-risk value scores ride along; value_judge.py turns them into the
    # send-back and the points. Optional, so a verdict without them still parses.
    for k in ("scores", "research", "beneficiary"):
        if isinstance(obj.get(k), dict):
            out[k] = obj[k]
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    raw = sys.stdin.read()
    out = parse(raw, need_metric="--need-metric" in argv)
    json.dump(out, sys.stdout)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
