#!/usr/bin/env python3
"""push_budget_gate.py -- no push to the philanthropy repo once today's Actions money is spent.

Reif, 2026-10-17 11:44 AM: "4,700 minutes in a day, when we know we are over." Every push to a
PR branch runs CI, and CI minutes are money. So the fleet reads the GitHub Actions meter before
EVERY push to the philanthropy repo:

  day share = (budget left this month) / (days left in the month)   e.g. $175 / 14 = $12.50
  spent     = today's actions netAmount from the org billing API    (10-minute cache file)

  under 80% of the share  -> any PR pushes
  80% to 100%             -> only a PR whose body's first line is `Moves: claims`,
                             `Moves: $150 report sales` or `Moves: jobs` (Jobs is priority 1,
                             INTENT 10-18 jobs-pushes-skip-gate; no PR yet: the head commit's body)
  100% and over           -> nothing pushes until the next UTC day

The only bypass is a deploy-gate revert: `push_budget_gate.py --why "deploy-gate revert ..."
-- git push ...`. Every verdict is a row in $FLEET_LOG_DIR/push_gate.jsonl; a push that lands
at or past the cap is a fleet bug, and tank.py and retro.py print the count.

Budget: $FLEET_ACTIONS_BUDGET_USD (the month's enterprise Actions budget) when set, else the
org's own Actions budget from the billing API. An unreadable meter lets the push through and
logs `unread`: a push is never stranded on a failed read, and the row says so.

Used three ways: as a PreToolUse hook (JSON payload on stdin, exit 2 = block), as
`--check [--wt DIR]` from scripts that push (worktree_builder.sh, minion_checkpoint.py), and
as the `--why` wrapper above. `--dispatch <items>` (dispatch_member.sh) holds a minion batch
that could only be refused.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ORG = "PhilanthropyOrg"
REPO_RX = re.compile(r"PhilanthropyOrg/philanthropy(\.git)?/?$")
CACHE_S = 600
SOFT_PCT, HARD_PCT = 80.0, 100.0
MONEY_LINES = ("Moves: claims", "Moves: $150 report sales", "Moves: jobs")


def log_dir() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _actions_usd(path: str) -> float | None:
    out = _run(["gh", "api", path, "--jq",
                '[.usageItems[] | select(.product=="actions") | .netAmount] | add // 0'])
    try:
        return float(out)
    except ValueError:
        return None


def _budget_from_fleet_env() -> str:
    """FLEET_ACTIONS_BUDGET_USD read straight from fleet.env when the process env lacks it.

    2026-10-17: the hook runs inside `claude -p`; a launch that did not source fleet.env (G's
    podman exec, an ad-hoc --summary) saw no budget, fell back to the org's $150, computed a
    $6.26 share and WROTE THAT INTO THE SHARED 10-MINUTE CACHE, so every member read 276%
    and nothing pushed. The file is the source of truth; read it before guessing."""
    for cand in (os.environ.get("FLEET_ENV_FILE", ""), "/fleet-kit/fleet.env", "fleet.env"):
        if not cand:
            continue
        try:
            for line in open(cand, encoding="utf-8"):
                line = line.strip()
                if line.startswith("FLEET_ACTIONS_BUDGET_USD="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _budget_usd() -> float | None:
    env = os.environ.get("FLEET_ACTIONS_BUDGET_USD", "").strip() or _budget_from_fleet_env()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    out = _run(["gh", "api", f"orgs/{ORG}/settings/billing/budgets", "--jq",
                '[.budgets[] | select(.budget_product_sku=="actions") | .budget_amount][0]'])
    try:
        return float(out)
    except ValueError:
        return None


def day_share(budget: float, month_spent_before_today: float, now: float) -> float:
    """Budget left at the start of today over the days left after today (Reif: $175 / 14)."""
    t = time.gmtime(now)
    days_left = max(1, calendar.monthrange(t.tm_year, t.tm_mon)[1] - t.tm_mday)
    return max(0.0, budget - month_spent_before_today) / days_left


def read_meter(now: float, fetch=None) -> dict | None:
    """{'day', 'share', 'spent', 'pct'} for today (UTC), or None when unreadable.

    `fetch` returns (budget, month_total, today_total) -- tests pass a fake meter."""
    t = time.gmtime(now)
    day = time.strftime("%Y-%m-%d", t)
    cache = log_dir() / "push_gate_meter.json"
    try:
        m = json.loads(cache.read_text())
        if m.get("day") == day and now - float(m.get("ts", 0)) < CACHE_S:
            return m
    except (OSError, ValueError):
        pass
    if fetch is None:
        base = f"orgs/{ORG}/settings/billing/usage?year={t.tm_year}&month={t.tm_mon}"
        fetch = lambda: (_budget_usd(), _actions_usd(base), _actions_usd(f"{base}&day={t.tm_mday}"))  # noqa: E731
    budget, month, today = fetch()
    if budget is None or month is None or today is None:
        return None
    share = day_share(budget, month - today, now)
    pct = 100.0 * today / share if share > 0 else 999.0
    m = {"ts": now, "day": day, "share": round(share, 2), "spent": round(today, 2), "pct": round(pct, 1)}
    try:
        cache.write_text(json.dumps(m))
    except OSError:
        pass
    return m


JOBS_P1 = ("13056", "13057", "13058", "13059")  # Reif 10-18 9:11 PM: Jobs P1 lane past 100%


def jobs_p1_issue(body: str) -> str:
    """The Jobs P1 issue a claims PR closes or is part of, else ''."""
    m = re.search(r"(?:Closes|Fixes|Resolves|Part of)\s+#(130(?:5[6-9]))\b", body or "", re.I)
    return m.group(1) if m and m.group(1) in JOBS_P1 else ""


def verdict(pct: float, moves: str, why: str = "", body: str = "") -> tuple[bool, str]:
    """(allowed, reason). The whole rule, in one place."""
    # Jobs P1 lane (Reif 10-18 9:11 PM): a claims PR that closes #13056-#13059 pushes past 100%, logged as
    # bypass. The $250 month wall stays hard: GitHub's enterprise budget stop refuses every run past it.
    if pct >= HARD_PCT and moves.strip().startswith("Moves: claims") and jobs_p1_issue(body):
        return True, "bypass"
    if why:
        if "revert" not in why.lower():
            return False, "--why is only for a deploy-gate revert"
        return True, "bypass"
    if pct >= HARD_PCT:
        return False, (f"today's Actions money is spent ({pct:.0f}% of the day's share): "
                       "nothing pushes to philanthropy until the next UTC day")
    if pct >= SOFT_PCT and not moves.strip().startswith(MONEY_LINES):
        return False, (f"{pct:.0f}% of today's Actions share is spent: only a PR whose body starts "
                       "`Moves: claims`, `Moves: $150 report sales` or `Moves: jobs` may push")
    return True, "ok"


def pr_body(wt: str) -> str:
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    body = _run(["gh", "pr", "view", branch, "--json", "body", "--jq", ".body"], cwd=wt) if branch else ""
    return body or _run(["git", "log", "-1", "--format=%B"], cwd=wt)


def moves_line(wt: str) -> str:
    """First line of the branch's open PR body, else the first body line of the head commit."""
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    body = _run(["gh", "pr", "view", branch, "--json", "body", "--jq", ".body"], cwd=wt) if branch else ""
    if not body:
        body = _run(["git", "log", "-1", "--format=%b"], cwd=wt)
    return next((ln.strip() for ln in body.splitlines() if ln.strip()), "")


def targets_philanthropy(wt: str) -> bool:
    return bool(REPO_RX.search(_run(["git", "remote", "get-url", "origin"], cwd=wt)))


def record(row: dict) -> None:
    try:
        with (log_dir() / "push_gate.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def check(wt: str, why: str = "", now: float | None = None, meter: dict | None = None,
          moves: str | None = None) -> tuple[bool, str]:
    """Gate one push from worktree `wt`. Logs the verdict; returns (allowed, reason)."""
    now = time.time() if now is None else now
    if not targets_philanthropy(wt):
        record({"ts": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now)), "member": os.environ.get(
            "FLEET_MEMBER", ""), "wt": wt, "verdict": "skip", "why": "not the philanthropy repo"})
        return True, "not the philanthropy repo"
    meter = meter if meter is not None else read_meter(now)
    moves = moves_line(wt) if moves is None else moves
    row = {"ts": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now)), "member": os.environ.get(
        "FLEET_MEMBER", ""), "wt": wt, "moves": moves[:60], "why": why[:200]}
    if meter is None:
        record({**row, "verdict": "unread"})
        return True, "meter unreadable (logged)"
    body = pr_body(wt)
    ok, reason = verdict(meter["pct"], moves, why, body)
    if reason == "bypass" and not why:
        row["why"] = f"jobs-p1-lane #{jobs_p1_issue(body)}"
    tag = "bypass" if reason == "bypass" else ("allow" if ok else "refuse")
    record({**row, "verdict": tag, "pct": meter["pct"], "share": meter["share"], "spent": meter["spent"],
            "past_cap": ok and meter["pct"] >= HARD_PCT})
    return ok, reason


def dispatch_hold(items: str, now: float, meter: dict | None = None) -> str:
    """Why a minion batch should not start now, or '' to start it. A minion's whole product is a
    push; past 100% every push is refused until 00:00 UTC. 2026-10-22: 25 minion passes ($22)
    ran past the cap, #13402 was rebuilt 4x and #13600 resumed 3x, none pushed. Jobs P1 items
    keep their bypass; an unread meter holds nothing."""
    meter = meter if meter is not None else read_meter(now)
    if not meter or meter["pct"] < HARD_PCT:
        return ""
    if set(re.findall(r"\d+", items)) & set(JOBS_P1):
        return ""
    return (f"push gate at {meter['pct']:.0f}% of today's Actions share; a minion could not push "
            "before 00:00 UTC")


def today_counts(now: float, path: Path | None = None) -> dict[str, int]:
    """Today's verdict counts plus `past_cap`, for tank.py and retro.py."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    n: dict[str, int] = {}
    try:
        lines = (path or log_dir() / "push_gate.jsonl").read_text().splitlines()
    except OSError:
        return n
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("day") == day:
            n[r.get("verdict", "?")] = n.get(r.get("verdict", "?"), 0) + 1
            n["past_cap"] = n.get("past_cap", 0) + bool(r.get("past_cap"))
    return n


def summary(now: float) -> str:
    """One line: today's share, spend, and what the gate did. A past-cap push is a fleet bug."""
    m = read_meter(now)
    n = today_counts(now)
    head = (f"${m['spent']:.2f} of ${m['share']:.2f} today ({m['pct']:.0f}%)" if m
            else "UNREADABLE Actions meter")
    line = (f"push gate: {head}; {n.get('allow', 0)} pushed, {n.get('refuse', 0)} refused, "
            f"{n.get('bypass', 0)} bypass")
    if n.get("past_cap"):
        line += f"; FLEET BUG: {n['past_cap']} push(es) past the cap"
    return line


def _push_segments(command: str) -> list[str]:
    return [s for s in re.split(r"[;&|]+", command) if re.search(r"\bgit\b(\s+-C\s+\S+)?\s+push\b", s)]


def decide(payload: dict, env: dict) -> str | None:
    """PreToolUse: None = allow, else the reason to block."""
    if payload.get("tool_name") != "Bash":
        return None
    from worktree_guard_hook import _strip_prose
    command = _strip_prose((payload.get("tool_input") or {}).get("command") or "")
    # The --why wrapper gates itself; only a bare `git push` is judged here.
    segs = [s for s in _push_segments(command) if "push_budget_gate.py" not in s]
    if not segs:
        return None
    wt = (env.get("WT_PATH") or "").strip() or payload.get("cwd") or os.getcwd()
    m = re.search(r"\bgit\s+-C\s+(\S+)", segs[0])
    ok, reason = check(m.group(1).strip("'\"") if m else wt)
    return None if ok else f"BLOCKED (push_budget_gate): {reason}."


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if argv[:1] == ["--summary"]:
        print(summary(time.time()))
        return 0
    if argv[:1] == ["--dispatch"]:
        why = dispatch_hold(" ".join(argv[1:]), time.time())
        if why:
            print(why)
        return 3 if why else 0
    if argv[:1] == ["--check"]:
        wt = argv[argv.index("--wt") + 1] if "--wt" in argv else os.getcwd()
        ok, reason = check(wt)
        if not ok:
            print(f"push_budget_gate: {reason}", file=sys.stderr)
        return 0 if ok else 3
    if argv[:1] == ["--why"]:
        if "--" not in argv or len(argv) < 4:
            print('usage: push_budget_gate.py --why "deploy-gate revert <why>" -- git push ...', file=sys.stderr)
            return 2
        ok, reason = check(os.getcwd(), why=argv[1])
        if not ok:
            print(f"push_budget_gate: {reason}", file=sys.stderr)
            return 3
        env = {**os.environ, "PUSH_BUDGET_GATE_BYPASS": "1"}  # the git-level pre-push hook honours the logged bypass
        return subprocess.run(argv[argv.index("--") + 1:], env=env).returncode
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    reason = decide(payload, os.environ)
    if reason is None:
        return 0
    import hook_blocks
    hook_blocks.record(payload, "push_budget_gate", reason)
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
