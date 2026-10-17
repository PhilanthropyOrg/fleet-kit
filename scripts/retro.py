#!/usr/bin/env python3
"""retro -- G's daily retro: what Reif had to say yesterday, and what the fleet did about it.

Reif, 2026-10-17: "I don't want to keep having to explain this daily. I want G to self improve
and cause the fleet to self improve." Each morning at 7:00 Central (entrypoint.sh cron) this
writes /fleet-kit/shares/g/retro-<today>.md, 10 lines max, about yesterday:

  1. what Reif said that the fleet should have found ($FLEET_LOG_DIR/INTENT.md dated lines,
     else the desk's copies in /fleet-kit/shares/g/inbox-copy/<yyyymmdd>-*.md)
  2. which lane should have caught each one (auditor, sentry or persona walk)
  3. the one charter change made (the newest predict.py row made yesterday)
  4. product numbers, yesterday vs the day before (/990/api/signals/kpi daily series)
  5. budget used vs the week gone, per account (/fleet-kit/shares/g/meter.json)
  6. the audit scoreboard (auditor vs Viken vs by hand; target 10-24: auditor > Viken)

G's host cron appends the file to ~/G/outbox.md. A missing input prints "no reading", never 0.

Usage: retro.py [--day YYYY-MM-DD] [--out DIR] [--print]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fleet_tz import CENTRAL  # noqa: E402

SHARES = Path("/fleet-kit/shares/g")
SCOREBOARD = SHARES / "audit-scoreboard.md"  # written by audit_scoreboard.py (fk#1687)
MAX_LINES = 10
# The lane that should have caught a direction. auditor checks a page is TRUE, sentry that it
# LOADS; anything else is how a person moves through it (a persona walk).
LANES = (
    ("sentry", ("down", "error", "slow", "broken", "redirect", "404", "500", "timeout", "load")),
    ("auditor", ("wrong", "count", "number", "shows", "says", "address", "hospital", "cap",
                 "label", "missing", "duplicate", "data", "should not", "never")),
)


def log_dir() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))


def directions(day: str, intent: str, inbox: Path) -> list[str]:
    """Reif's words from `day`: INTENT.md's dated lines, else the desk's inbox copies."""
    said = re.findall(rf'^- {day} "([^"]+)"', intent, re.M)
    if said or not inbox.is_dir():
        return said
    out = []
    for p in sorted(inbox.glob(f"{day.replace('-', '')}-*.md")):
        first = p.read_text(errors="ignore").strip().split("\n", 1)[0]
        out.append(first.split(". ")[0][:90])
    return out


def lane_for(text: str) -> str:
    low = text.lower()
    for lane, words in LANES:
        if any(w in low for w in words):
            return lane
    return "persona walk"


def charter_change(rows: list[dict], day: str) -> str:
    made = [r for r in rows
            if dt.datetime.fromtimestamp(float(r.get("ts") or 0), CENTRAL).strftime("%F") == day]
    if not made:
        return "none registered"
    r = max(made, key=lambda r: float(r["ts"]))
    return f"#{r['id']} {r['member']} {r['change']}: {r.get('note', '')[:80]}"


def _day_n(series: list, day: str, key: str = "n"):
    for row in series or []:
        if row.get("d") == day:
            return row.get(key)
    return None


def scoreboard(kpi: dict | None, day: str, before: str) -> str:
    if not kpi:
        return "no reading (/990/api/signals/kpi unreachable)"
    claims = (kpi.get("claims") or {}).get("daily_30d") or []
    pairs = [
        ("claims filed", _day_n(claims, day, "submitted"), _day_n(claims, before, "submitted")),
        ("approved", _day_n(claims, day, "approved"), _day_n(claims, before, "approved")),
        ("Scout+ sales", *(_day_n((kpi.get("revenue") or {}).get("new_subscriptions_daily_30d"), d)
                           for d in (day, before))),
        ("signups", *(_day_n((kpi.get("signups") or {}).get("daily_30d"), d) for d in (day, before))),
    ]
    out = [f"{name} {'?' if a is None else a} (was {'?' if b is None else b})" for name, a, b in pairs]
    verified = (kpi.get("claims") or {}).get("verified_orgs")
    applies = ((kpi.get("jobs") or {}).get("applications") or {}).get("last_7d")
    out.append(f"paid verified {'?' if verified is None else verified}")
    out.append(f"job applies 7d {'?' if applies is None else applies}")
    return ", ".join(out)


def budget(meter: dict | None) -> str:
    if not meter:
        return "no reading (meter.json missing)"
    out = []
    for r in meter.get("rows") or []:
        if "week_used" in r:
            out.append(f"{r['name']} {r['week_used']}% used, {r.get('elapsed', '?')}% of week gone")
        else:
            out.append(f"{r['name']} no reading")
    return "; ".join(out) or "no reading"


def push_gate_line(day: dt.date) -> str:
    """Yesterday's push gate verdicts; a push past the Actions cap is a fleet bug (Reif 10-17)."""
    import push_budget_gate
    n = push_budget_gate.today_counts(dt.datetime(day.year, day.month, day.day, 12).timestamp())
    line = f"push gate {n.get('allow', 0)} pushed, {n.get('refuse', 0)} refused"
    return line + (f", FLEET BUG {n['past_cap']} past the cap" if n.get("past_cap") else "")


def audit_counts(text: str) -> dict[str, int]:
    """{'Auditor': n, 'Viken': n, 'By hand': n} from the scoreboard table; {} when absent."""
    n = {k: int(v) for k, v in re.findall(r"^\| (Auditor|Viken|By hand)[^|]*\| (\d+) \|", text, re.M)}
    return n if {"Auditor", "Viken"} <= n.keys() else {}


def audit(text: str) -> str:
    n = audit_counts(text)
    if not n:
        return "no scoreboard yet"
    a, v = n["Auditor"], n["Viken"]
    goal = "ahead" if a > v else f"behind by {v - a + 1}"
    return (f"auditor {a}, Viken {v}, by hand {n.get('By hand', 0)} this week "
            f"(target 10-24: auditor > Viken; {goal})")


def render(day: str, said: list[str], change: str, numbers: str, money: str, holes: str) -> str:
    lines = [f"# Retro {day} (G, 7:00 Central)"]
    if said:
        lines.append(f"Reif had to say {len(said)} thing(s) the fleet should have found:")
        lines += [f'- "{s[:90]}" -> missed by {lane_for(s)}' for s in said[:3]]
        if len(said) > 3:
            lines.append(f"- and {len(said) - 3} more in INTENT.md")
    else:
        lines.append("Reif had to say nothing new yesterday.")
    lines += [f"Charter change: {change}", f"Product: {numbers}", f"Budget: {money}",
              f"Audit: {holes}"]
    assert len(lines) <= MAX_LINES, lines
    return "\n".join(lines) + "\n"


def read_text(p: Path) -> str:
    try:
        return p.read_text(errors="ignore")
    except OSError:
        return ""


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _rows(p: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _kpi() -> dict | None:
    import jobs_daily
    try:
        return jobs_daily.fetch_kpi()
    except Exception:  # noqa: BLE001 -- any failed read is "no reading", never a crash
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="the day to look back on (default: yesterday, Central)")
    ap.add_argument("--out", type=Path, default=SHARES)
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args(argv)
    today = dt.datetime.now(CENTRAL).date()
    day = a.day or str(today - dt.timedelta(days=1))
    before = str(dt.date.fromisoformat(day) - dt.timedelta(days=1))
    text = render(
        day,
        directions(day, read_text(log_dir() / "INTENT.md"), SHARES / "inbox-copy"),
        charter_change(_rows(log_dir() / "predictions.jsonl"), day),
        scoreboard(_kpi(), day, before),
        budget(_read_json(SHARES / "meter.json") or _read_json(SHARES / "meter-snapshot.json"))
        + "; " + push_gate_line(dt.date.fromisoformat(day)),
        audit(read_text(SCOREBOARD)),
    )
    out = a.out / f"retro-{dt.date.fromisoformat(day) + dt.timedelta(days=1)}.md"
    out.write_text(text)
    print(text if a.print else f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
