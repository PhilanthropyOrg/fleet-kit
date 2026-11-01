#!/usr/bin/env python3
"""value_judge -- turn judge-judy's four-risk scores into a pass/send-back and value points.

Reif, 2026-10-30 (PHIA-1451): "a single judge that can decide -- based on its own research ...
the judge can and should routinely send back PRs that don't create value." The standard is
Marty Cagan's four big risks (SVPG, "Inspired"): Value, Usability, Feasibility, Viability, each
scored 0-3 with one line of why, plus a research check. The model scores; this script decides,
so the send-back rule and the points formula are code a test can pin, not prose a model may drift.

  points    = value x mean(usability, feasibility, viability)      (0..9)
              (usability left out when nothing a person sees changes)
  send back = value <= 1, usability 0, a thin value card, or points < JUDGE_MIN_POINTS (default 3)

Paul, 2026-11-01 (calibration): 45 of 51 verdicts scored exactly 2/2/2/2 and 0 were sent back for
value. The judge now names the user and the user number it expects to move (`beneficiary`); a blank
user or number caps Value at 1, and Value 1 is a send-back. The bar moved from 1.5 to 3 points, so
Value 2 needs the other three to average 1.5 or more.

PHIA-1917 (calibration, 11-01): 13 of 19 verdicts were still 2/2/2/2, much of it Usability 2,
which meant two things: "nothing a person sees changes" and "a visible change that follows the
rules". The judge now says which (`usability.visible`). Nothing visible is shown as n/a and left
out of the points, so a Usability 2 means one thing: visible, and it follows the design rules.
A stronger model re-judges 1 head in JUDGE_CALIBRATE_EVERY (default 10, picked by head sha) and
the pair goes to judge-judy-calibration.jsonl; `calibration` reports where the two disagree.

Paul, 2026-10-31 (value gate): the why and the research come from the value card on the task
(scripts/value_card.py), never from a set section in the PR body -- 10 of 10 sampled vertical
PRs had no `## Research`, so requiring one would be a paperwork block with a new name. A card
the judge finds thin sends the PR back with "edit the card", and the judge re-reads the card
with no new commit. A PR whose task has no card is not held unless JUDGE_CARD_REQUIRED=1. The
judge scores blind: the self-score is cut from the card before the model sees it, and the gap
is logged here afterwards. The seat is the task's owner on the board, not the branch prefix.

Paul, 2026-10-31 ("check before building"): a seat posts a plan with its own four-risk
self-score before it writes code (on the card; a PR-body line like
  Plan: Value 2, Usability 3, Feasibility 2, Viability 2
is read too when there is no card). Each review records whether the PR had a plan and whether the self-score matched the judge's
(every risk within 1), so the ledger answers "are seats learning to judge?" and "how many
send-backs never had a plan?".

Usage:
  value_judge.py apply --body BODY_FILE --pr N --head SHA --title T --ref BRANCH [--exempt] [--card CARD_JSON]
      stdin: judge_judy_verdict.py's output. stdout: the same object with verdict, findings_text
      and a `ledger` row folded in. Exit 0 always; an unscored verdict passes through unchanged.
  value_judge.py sample --head SHA        exit 0 when this head is in the 1-in-N stronger re-judge
  value_judge.py pair --base LEDGER_ROW --model M   stdin: the stronger model's parsed verdict;
      stdout: one calibration row (both score sets) for judge-judy-calibration.jsonl
  value_judge.py calibration CAL_LEDGER [--days 7]   agreement between the judge and the stronger model
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
MIN_POINTS = float(os.environ.get("JUDGE_MIN_POINTS", "3"))
MIN_VALUE = int(os.environ.get("JUDGE_MIN_VALUE", "2"))  # Value below this is a send-back
CARD_REQUIRED = os.environ.get("JUDGE_CARD_REQUIRED", "0") == "1"
CALIBRATE_EVERY = int(os.environ.get("JUDGE_CALIBRATE_EVERY", "10"))  # 0 turns the re-judge off

# The schema fragment judge-judy.sh merges into VERDICT_SCHEMA (kept here so one file owns it).
_RISK = {"type": "object", "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 3},
                                          "why": {"type": "string"}}, "required": ["score", "why"]}
# Usability also says whether a person sees anything change (PHIA-1917).
_USABILITY = {"type": "object", "properties": {**_RISK["properties"], "visible": {"type": "boolean"}},
              "required": ["score", "why", "visible"]}
SCORES_SCHEMA = {
    "scores": {"type": "object", "properties": {r: (_USABILITY if r == "usability" else _RISK) for r in RISKS},
               "required": list(RISKS)},
    "research": {"type": "object", "properties": {"ok": {"type": "boolean"}, "why": {"type": "string"}},
                 "required": ["ok", "why"]},
    "beneficiary": {"type": "object", "properties": {"user": {"type": "string"}, "number": {"type": "string"}},
                    "required": ["user", "number"]},
}
_BLANK = {"", "none", "n/a", "na", "unknown", "-", "nobody", "no one"}


def named(beneficiary) -> bool:
    """True when the judge named both a user and the user number this PR should move."""
    b = beneficiary if isinstance(beneficiary, dict) else {}
    return all(str(b.get(k, "")).strip().lower().strip(".") not in _BLANK for k in ("user", "number"))

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


def points(scores: dict, visible: bool = True) -> float:
    rest = [scores[r] for r in RISKS[1:] if visible or r != "usability"]
    return round(scores["value"] * sum(rest) / len(rest), 2)


def invisible(verdict: dict, nums: dict) -> bool:
    """Nothing a person sees changes: the judge said visible false and did not mark it down.
    A 0 or 1 always counts (a slower or broken page is something a person meets)."""
    u = (verdict.get("scores") or {}).get("usability")
    return isinstance(u, dict) and u.get("visible") is False and nums["usability"] >= 2


def sampled(head: str, every: int = CALIBRATE_EVERY) -> bool:
    """1 head in `every`, by the head sha, so a re-run of the same head picks the same answer."""
    try:
        return every > 0 and int((head or "")[:8], 16) % every == 0
    except ValueError:
        return False


def judge(scores: dict, research_ok: bool, card: dict | None = None, research_why: str = "",
          visible: bool = True) -> list:
    """Reasons to send the PR back; empty means it passes on value.

    Research is judged from the task's value card. No card -> no research send-back (unless
    JUDGE_CARD_REQUIRED and the board answered); a thin card -> edit the card, no new commit."""
    card = card or {}
    task = card.get("task") or "the task"
    reasons = []
    if scores["value"] == 0:
        reasons.append("Value is 0: no person gets something they came for, saves a step, or loses dead weight.")
    elif scores["value"] < MIN_VALUE:
        reasons.append(f"Value is {scores['value']}: no named user and user number this moves enough to ship. "
                       "Name who gets it and the number it moves (on the task), or fold it into work that does.")
    if scores["usability"] == 0:
        reasons.append("Usability is 0: a person could not use what this changes.")
    if card.get("card") and not research_ok:
        reasons.append(f"The value card on {task} is thin: {research_why or 'best in class is missing'}. "
                       "Edit the card (Best in class: who, link, how we match or beat it); "
                       "the judge reads it again with no new commit.")
    elif card and not card.get("card") and CARD_REQUIRED:
        reasons.append(f"No value card on {task}: add one (User, Need, Data, Guess, Best in class, self-score) "
                       "as the task document 'value-card'; the judge reads it again with no new commit.")
    p = points(scores, visible)
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


def scores_text(nums, whys, research, pts, plan, reasons, card=None, beneficiary=None, visible=True) -> str:
    lines = []
    if beneficiary is not None:
        lines += [f"**Who gets it:** {beneficiary.get('user') or '(not named)'}. "
                  f"**Number it moves:** {beneficiary.get('number') or '(not named)'}.", ""]
    lines += ["| Risk | Score | Why |", "|---|---|---|"]
    lines += [f"| {r.title()} | {'n/a, nothing a person sees' if r == 'usability' and not visible else f'{nums[r]}/3'}"
              f" | {whys[r]} |" for r in RISKS]
    card = card or {}
    src = "card" if card.get("card") else (f"card unread: {card['error']}" if card.get("error")
                                           else "task as card" if card.get("task_text") else "no card")
    lines.append(f"| Research ({src}) | {'yes' if research.get('ok') else 'no'} | {research.get('why', '')} |")
    lines.append("")
    lines.append(f"**Value points: {pts}** (Value x average of the other three"
                 + (", Usability left out)." if not visible else ")."))
    if plan:
        lines.append("Plan self-score: " + ", ".join(f"{r.title()} {plan[r]}" for r in RISKS)
                     + (" -- matches the judge." if plan_matches(plan, nums) else " -- differs from the judge by more than 1."))
    else:
        lines.append("No self-score on the value card (scored blind either way).")
    if reasons:
        lines.append("")
        lines.append("**Sent back on value:**")
        lines += [f"- {r}" for r in reasons]
    return "\n".join(lines)


def plan_matches(plan, nums) -> bool:
    return all(abs(plan[r] - nums[r]) <= 1 for r in RISKS)


def apply(verdict: dict, body: str, pr: str, head: str, title: str, ref: str, exempt: bool,
          card: dict | None = None) -> dict:
    card = card if isinstance(card, dict) else {}
    out = dict(verdict)
    if not verdict.get("ok"):
        return out
    nums, whys = _scores_of(verdict)
    if nums is None:
        return out
    beneficiary = verdict.get("beneficiary") if isinstance(verdict.get("beneficiary"), dict) else {}
    if not named(beneficiary) and nums["value"] > 1:  # no named user and number -> Value 1 at most
        whys = dict(whys, value=whys["value"] + f" (capped at 1 from {nums['value']}: no named user and number)")
        nums = dict(nums, value=1)
    research = verdict.get("research") if isinstance(verdict.get("research"), dict) else {}
    research_ok = bool(research.get("ok"))
    visible = not invisible(verdict, nums)
    pts = points(nums, visible)
    plan = plan_score("plan " + card["plan"]) if card.get("plan") else plan_score(body)
    reasons = [] if exempt else judge(nums, research_ok, card, str(research.get("why", "")), visible)
    correctness = verdict.get("verdict")
    final = "block" if (correctness == "block" or reasons) else "approve"
    text = scores_text(nums, whys, research, pts, plan, reasons, card, beneficiary, visible)
    out["verdict"] = final
    out["scores_text"] = text
    out["findings_text"] = (verdict.get("findings_text") or "") + ("\n\n" if verdict.get("findings_text") else "") + text
    out["ledger"] = {
        "ts": time.time(), "pr": pr, "head": head, "seat": card.get("owner") or seat_of(ref),
        "task": card.get("task") or task_of(title, ref), "card": bool(card.get("card")),
        "scores": nums, "research_ok": research_ok, "points": pts, "verdict": final,
        "correctness": correctness, "value_reasons": reasons, "exempt": exempt,
        "plan": plan, "plan_matched": plan_matches(plan, nums) if plan else None,
        "user": str(beneficiary.get("user", "")), "number": str(beneficiary.get("number", "")),
        "usability_visible": visible,
    }
    return out


def pair(base: dict, strong: dict, model: str) -> dict | None:
    """One calibration row: the judge's ledger row next to the stronger model's scores, same
    prompt, same head. The stronger verdict decides nothing; it is only read on Friday."""
    if not strong.get("ok"):
        return None
    applied = apply(strong, "", base.get("pr", ""), base.get("head", ""), "", "", bool(base.get("exempt")),
                    {"task": base.get("task"), "owner": base.get("seat"), "card": "x" if base.get("card") else ""})
    row = applied.get("ledger")
    if not row:
        return None
    return {"ts": time.time(), "pr": base.get("pr"), "head": base.get("head"), "seat": base.get("seat"),
            "model": model, "judge": {k: base.get(k) for k in ("scores", "points", "verdict", "usability_visible", "user", "number")},
            "strong": {k: row.get(k) for k in ("scores", "points", "verdict", "usability_visible", "user", "number")}}


def calibration(rows: list, days: int = 7, now=None) -> dict:
    """Where the judge and the stronger model disagree. A risk 'differs' when the scores are 2+ apart."""
    now = now or time.time()
    rows = [r for r in rows if r.get("ts", 0) >= now - days * 86400 and r.get("judge", {}).get("scores")
            and r.get("strong", {}).get("scores")]
    n = len(rows)
    if not n:
        return {"pairs": 0}
    gap = {k: round(sum(abs(r["judge"]["scores"][k] - r["strong"]["scores"][k]) for r in rows) / n, 2) for k in RISKS}
    clump = lambda s: all(s[k] == 2 for k in RISKS)
    return {
        "pairs": n,
        "verdict_agree": round(sum(r["judge"]["verdict"] == r["strong"]["verdict"] for r in rows) / n, 3),
        "mean_gap_per_risk": gap,
        "differs_by_2_or_more": [{"pr": r["pr"], "judge": r["judge"]["scores"], "strong": r["strong"]["scores"]}
                                 for r in rows if any(abs(r["judge"]["scores"][k] - r["strong"]["scores"][k]) >= 2 for k in RISKS)],
        "all_2s": {"judge": sum(clump(r["judge"]["scores"]) for r in rows), "strong": sum(clump(r["strong"]["scores"]) for r in rows)},
        "points_mean": {"judge": round(sum(r["judge"]["points"] for r in rows) / n, 2),
                        "strong": round(sum(r["strong"]["points"] for r in rows) / n, 2)},
    }


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
    a.add_argument("--card", default="")
    s = sub.add_parser("summary")
    s.add_argument("ledger")
    s.add_argument("--merged", default=None)
    s.add_argument("--days", type=int, default=7)
    sub.add_parser("schema")
    sm = sub.add_parser("sample")
    sm.add_argument("--head", required=True)
    pa = sub.add_parser("pair")
    pa.add_argument("--base", required=True)
    pa.add_argument("--model", default="")
    c = sub.add_parser("calibration")
    c.add_argument("ledger")
    c.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    if args.cmd == "schema":
        json.dump(SCORES_SCHEMA, sys.stdout)
        return 0
    if args.cmd == "sample":
        return 0 if sampled(args.head) else 1
    if args.cmd == "pair":
        try:
            with open(args.base, encoding="utf-8") as f:
                base = json.load(f)
            strong = json.load(sys.stdin)
        except (OSError, json.JSONDecodeError):
            return 0
        row = pair(base, strong, args.model)
        if row:
            print(json.dumps(row))
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
        card = {}
        if args.card:
            try:
                with open(args.card, encoding="utf-8") as f:
                    card = json.load(f)
            except (OSError, json.JSONDecodeError):
                card = {}
        json.dump(apply(verdict, body, args.pr, args.head, args.title, args.ref, args.exempt, card), sys.stdout)
        return 0
    rows = []
    try:
        with open(args.ledger, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except OSError:
        pass
    if args.cmd == "calibration":
        json.dump(calibration(rows, args.days), sys.stdout, indent=2)
        print()
        return 0
    merged = set(args.merged.split(",")) if args.merged else None
    json.dump(summary(rows, merged, args.days), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
