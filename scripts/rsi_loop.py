"""rsi_loop -- the recursive-self-improvement loop as a table: step -> owner.

fk#1122 (Reif 2026-09-17, after fk#1116 deactivated jefe, dumbledore and vp): "given we
turned off a few members, make sure the fleet can still achieve rsi". A step whose only owner
is a disabled member silently stops while every governor reads green -- the failure class the
fleet exists to prevent. This table is the one place that names who owns each step, and
selftest fails the moment a named member is enabled:false or a named script has no cron line.

Owner kinds: "member" (a members/<name>/<name>.fleet.json, must be enabled), "dispatched"
(a member another member spawns with --item -- minion by gru -- whose own enabled flag is
false by design and whose liveness is its dispatcher's), or "script" (a scripts/<file>,
must appear in entrypoint.sh's crontab).
"""
from __future__ import annotations

STEPS: tuple[tuple[str, str, str], ...] = (
    # (step, owner kind, owner)
    ("observe: look at the console the way Reif does, file what he would", "script", "reif_eyes.py"),
    ("observe: walk the product like a person, file what stops working", "member", "sentry"),
    ("file: prod alerts and forwarded mail become board items", "member", "dont-shoot-the-messenger"),
    ("rank: every open item real, vision-ranked, PRD-shaped", "member", "marie"),
    ("build: top-ranked items become one PR", "member", "gru"),
    ("build: the PR itself (spawned by gru, never on cron)", "dispatched", "minion"),
    ("review: every open PR gets a verdict", "member", "judge-judy"),
    ("fix: red CI/deploy/prod, parked green PRs, day-stale red PRs", "member", "the-fixer"),
    ("accept: a world-class item is judged against its own bar", "member", "vp"),
    ("grade: the fleet scores its own week against the predictions ledger", "script", "self_improve_score.sh"),
    ("act on the grade: a flat or falling score becomes a filed item", "script", "rsi_stall_check.py"),
    ("learn: every Broken: line in a handoff becomes an owner issue", "script", "handoff.py file-broken"),
    ("prune the rules: charters accumulating patches get named", "script", "charter_bloat_check.py"),
)


def unowned(members: dict[str, bool], crontab: str) -> list[str]:
    """Steps whose owner cannot run: a member that is disabled or missing, or a script with
    no line in the crontab text. `members` maps name -> enabled."""
    out = []
    for step, kind, owner in STEPS:
        if kind == "member":
            if not members.get(owner, False):
                out.append(f"{step} -> member {owner} is {'disabled' if owner in members else 'missing'}")
        elif kind == "dispatched":
            if owner not in members:
                out.append(f"{step} -> dispatched member {owner} is missing")
        else:
            if owner not in crontab:
                out.append(f"{step} -> script {owner} has no cron line in entrypoint.sh")
    return out
