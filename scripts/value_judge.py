#!/usr/bin/env python3
"""value_judge -- turn judge-judy's four-risk scores into a pass/send-back and value points.

Reif, 2026-10-30 (PHIA-1451): "a single judge that can decide -- based on its own research ...
the judge can and should routinely send back PRs that don't create value." The standard is
Marty Cagan's four big risks (SVPG, "Inspired"): Value, Usability, Feasibility, Viability, each
scored 0-3 with one line of why, plus a research check. The model scores; this script decides,
so the send-back rule and the points formula are code a test can pin, not prose a model may drift.

  points    = value x mean(usability, feasibility, viability)      (0..9)
  send back = value 0, usability 0, no research, or points < JUDGE_MIN_POINTS (default 1.5)

Paul, 2026-10-31 ("check before building"): a seat posts a plan with its own four-risk
self-score before it writes code, and copies it into the PR body as a line like
  Plan: Value 2, Usability 3, Feasibility 2, Viability 2 (PHIA-123)
Each review records whether the PR had a plan and whether the self-score matched the judge's
(every risk within 1), so the ledger answers "are seats learning to judge?" and "how many
send-backs never had a plan?".

Usage:
  value_judge.py apply --body BODY_FILE --pr N --head SHA --title T --ref BRANCH [--exempt]
      stdin: judge_judy_verdict.py's output. stdout: the same object with verdict, findings_text
      and a `ledger` row folded in. Exit 0 always; an unscored verdict passes through unchanged.
  value_judge.py summary LEDGER [--merged N,N,...] [--days 7]
      points per seat per day from the ledger (latest review of each PR; only merged PRs count
      when --merged is given), plus send-back rate, plan-match share and planless send-backs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict

RISKS = ("value", "usability", "feasibility", "viability")
MIN_POINTS = float(os.environ.get("JUDGE_MIN_POINTS", "1.5"))

# The schema fragment judge-judy.sh merges into VERDICT_SCHEMA (kept here so one file owns it).
_RISK = {"type": "object", "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 3},
                                          "why": {"type": "string"}}, "required": ["score", "why"]}
SCORES_SCHEMA = {
    "scores": {"type": "object", "properties": {r: _RISK for r in RISKS}, "required": list(RISKS)},
    "research": {"type": "object", "properties": {"ok": {"type": "boolean"}, "why": {"type": "string"}},
                 "required": ["ok", "why"]},
}

_PLAN_RE = re.compile(
    r"plan[^\n]*?value\D{0,4}([0-3])\D+?usability\D{0,4}([0-3])\D+?feasibility\D{0,4}([0-3])"
    r"\D+?viability\D{0,4}([0-3])", re.IGNORECASE)
_TASK_RE = re.compile(r"\bPHIA-?(\d+)\b", re.IGNORECASE)


def plan_score(body: str):
    """The seat's own plan self-score from the PR body, or None when it carries no plan line."""
    m = _PLAN_RE.search(body or "")
    return dict(zip(RISKS, map(int, m.groups()))) if m else None


def seat_of(ref: str) -> str:
    """Seat PR branches are <seat>/<slug> (jobs/..., g/..., story/...); ops/ is Operations."""
    return ref.split("/", 1)[0] if "/" in (ref or "") else "unknown"


def task_of(title: str, ref: str):
    m = _TASK_RE.search(title or "") or _TASK_RE.search(ref or "")
    return f"PHIA-{m.group(1)}" if m else None


def points(scores: dict) -> float:
    rest = [scores[r] for r in RISKS[1:]]
    return round(scores["value"] * sum(rest) / len(rest), 2)


def judge(scores: dict, research_ok: bool) -> list:
    """Reasons to send the PR back; empty means it passes on value."""
    reasons = []
    if scores["value"] == 0:
        reasons.append("Value is 0: no person gets something they came for, saves a step, or loses dead weight.")
    if scores["usability"] == 0:
        reasons.append("Usability is 0: a person could not use what this changes.")
    if not research_ok:
        reasons.append("No research: the PR must name who does this best, with sources, and how it matches or beats them.")
    p = points(scores)
    if not reasons and p < MIN_POINTS:
        reasons.append(f"Too little value to ship: {p} points, the bar is {MIN_POINTS}.")
    return reasons


def _scores_of(obj: dict):
    s = obj.get("scores")
    if not isinstance(s, dict):
        return None, {}
    try:
        nums = {r: int(s[r]["score"]) for r in RISKS}
    except (KeyError, TypeError, ValueError):
        return None, {}
    if not all(0 <= n <= 3 for n in nums.values()):
        return None, {}
    return nums, {r: str(s[r].get("why", "")) for r in RISKS}


def scores_text(nums, whys, research, pts, plan, reasons) -> str:
    lines = ["| Risk | Score | Why |", "|---|---|---|"]
    lines += [f"| {r.title()} | {nums[r]}/3 | {whys[r]} |" for r in RISKS]
    lines.append(f"| Research | {'yes' if research.get('ok') else 'no'} | {research.get('why', '')} |")
    lines.append("")
    lines.append(f"**Value points: {pts}** (Value x average of the other three).")
    if plan:
        lines.append("Plan self-score: " + ", ".join(f"{r.title()} {plan[r]}" for r in RISKS)
                     + (" -- matches the judge." if plan_matches(plan, nums) else " -- differs from the judge by more than 1."))
    else:
        lines.append("No plan line in the PR body (Plan: Value n, Usability n, Feasibility n, Viability n).")
    if reasons:
        lines.append("")
        lines.append("**Sent back on value:**")
        lines += [f"- {r}" for r in reasons]
    return "\n".join(lines)


def plan_matches(plan, nums) -> bool:
    return all(abs(plan[r] - nums[r]) <= 1 for r in RISKS)


def apply(verdict: dict, body: str, pr: str, head: str, title: str, ref: str, exempt: bool) -> dict:
    out = dict(verdict)
    if not verdict.get("ok"):
        return out
    nums, whys = _scores_of(verdict)
    if nums is None:
        return out
    research = verdict.get("research") if isinstance(verdict.get("research"), dict) else {}
    research_ok = bool(research.get("ok"))
    pts = points(nums)
    plan = plan_score(body)
    reasons = [] if exempt else judge(nums, research_ok)
    correctness = verdict.get("verdict")
    final = "block" if (correctness == "block" or reasons) else "approve"
    text = scores_text(nums, whys, research, pts, plan, reasons)
    out["verdict"] = final
    out["scores_text"] = text
    out["findings_text"] = (verdict.get("findings_text") or "") + ("\n\n" if verdict.get("findings_text") else "") + text
    out["ledger"] = {
        "ts": time.time(), "pr": pr, "head": head, "seat": seat_of(ref), "task": task_of(title, ref),
        "scores": nums, "research_ok": research_ok, "points": pts, "verdict": final,
        "correctness": correctness, "value_reasons": reasons, "exempt": exempt,
        "plan": plan, "plan_matched": plan_matches(plan, nums) if plan else None,
    }
    return out


def summary(rows: list, merged=None, days: int = 7, now=None) -> dict:
    now = now or time.time()
    rows = [r for r in rows if r.get("ts", 0) >= now - days * 86400]
    latest = {}
    for r in sorted(rows, key=lambda r: r.get("ts", 0)):
        latest[str(r["pr"])] = r
    per_day = defaultdict(float)
    for pr, r in latest.items():
        if r["verdict"] != "approve" or (merged is not None and pr not in merged):
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
        per_day[(r["seat"], day)] += r["points"]
    sent_back = [r for r in rows if r.get("value_reasons")]
    planned = [r for r in rows if r.get("plan")]
    return {
        "points_per_seat_per_day": [{"seat": s, "day": d, "points": round(p, 2)}
                                    for (s, d), p in sorted(per_day.items())],
        "reviews": len(rows),
        "send_back_rate": round(sum(1 for r in rows if r["verdict"] == "block") / len(rows), 3) if rows else None,
        "value_send_backs": len(sent_back),
        "plan_match_share": round(sum(1 for r in planned if r.get("plan_matched")) / len(planned), 3) if planned else None,
        "sent_back_without_plan": sum(1 for r in sent_back if not r.get("plan")),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--body", required=True)
    a.add_argument("--pr", default="")
    a.add_argument("--head", default="")
    a.add_argument("--title", default="")
    a.add_argument("--ref", default="")
    a.add_argument("--exempt", action="store_true")
    s = sub.add_parser("summary")
    s.add_argument("ledger")
    s.add_argument("--merged", default=None)
    s.add_argument("--days", type=int, default=7)
    sub.add_parser("schema")
    args = ap.parse_args(argv)
    if args.cmd == "schema":
        json.dump(SCORES_SCHEMA, sys.stdout)
        return 0
    if args.cmd == "apply":
        try:
            verdict = json.load(sys.stdin)
        except json.JSONDecodeError:
            verdict = {"ok": False, "reason": "verdict was not JSON"}
        try:
            with open(args.body, encoding="utf-8", errors="replace") as f:
                body = f.read()
        except OSError:
            body = ""
        json.dump(apply(verdict, body, args.pr, args.head, args.title, args.ref, args.exempt), sys.stdout)
        return 0
    rows = []
    try:
        with open(args.ledger, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except OSError:
        pass
    merged = set(args.merged.split(",")) if args.merged else None
    json.dump(summary(rows, merged, args.days), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
