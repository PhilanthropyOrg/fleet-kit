#!/usr/bin/env python3
"""handoff.py -- what the team learned in the last day, read by every pass before it starts.

Reif, 2026-09-16: "the next run should be informed by the last set of runs, so the team self
learns. its not about me. and if the meter is broken, like it was for gru, then who fixes it
and how is that tracked?"

Before this, a member saw only its own rows in runs.jsonl if its charter told it to look, and
what another member learned an hour ago was invisible. gru reported "runway blind" three
passes running with the meter broken and nobody owning it.

Now:
  handoff.py write   -> $FLEET_LOG_DIR/HANDOFF.md: per member its last outcome, then every
                        `Lesson:` and `Broken:` line the fleet wrote in 24h, open asks, open
                        instrument issues. run_member.sh prepends it to every prompt.
  handoff.py file-broken -> one `instrument: <name>` issue per distinct Broken: line, in the
                        fleet-kit repo (the code the instrument lives in), priority-high,
                        idempotent by title. That issue is the owner and the tracking: it is
                        on the board, in the handoff, and in reif_eyes' stale check.

Both are deterministic, no model. The two new report lines (`Lesson:` / `Broken:`) are in
persona_law.md 10b and parsed by run_report.py.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
HANDOFF = LOG_DIR / "HANDOFF.md"
KIT_REPO = os.environ.get("FLEET_KIT_REPO", "PhilanthropyOrg/fleet-kit")
NONE_WORDS = {"", "none", "none.", "n/a", "nothing", "no", "-", "--"}


def _is_none(v) -> bool:
    return (v or "").strip().strip("`*").lower() in NONE_WORDS


def load_runs(path: pathlib.Path | None = None) -> list[dict]:
    p = path or (LOG_DIR / "runs.jsonl")
    rows = []
    try:
        for line in p.read_text(errors="ignore").splitlines()[-4000:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return rows


def _ago(ts: float, now: float) -> str:
    m = int((now - ts) // 60)
    return f"{m}m ago" if m < 90 else f"{m // 60}h ago"


def render(runs: list[dict], asks: list[dict], instrument_issues: list[dict], now: float, hours: float = 24.0) -> str:
    since = now - hours * 3600
    recent = [r for r in runs if (r.get("ts") or 0) >= since and r.get("member")]
    last: dict[str, dict] = {}
    for r in recent:
        if r.get("status") in ("started", "heartbeat", "dispatch_skipped"):
            continue
        if r["member"] not in last or r.get("ts", 0) > last[r["member"]].get("ts", 0):
            last[r["member"]] = r
    out = [f"# HANDOFF -- what the team learned in the last {int(hours)}h (written {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))})",
           "", "Read this before choosing what to do. A Broken: instrument below is someone's job, not a fact to cite again.", ""]
    out.append("## Where each member left off")
    for m, r in sorted(last.items(), key=lambda kv: -kv[1].get("ts", 0)):
        line = f"- **{m}** {_ago(r.get('ts', now), now)} · {r.get('status')}"
        if r.get("outcome"):
            line += f" · {str(r['outcome']).strip()[:160]}"
        out.append(line)
    if not last:
        out.append("- (no finished runs in the window)")
    lessons = [(r["member"], r["lesson"].strip(), r.get("ts", 0)) for r in recent if r.get("lesson") and not _is_none(r["lesson"])]
    out += ["", "## Lessons (newest first)"]
    out += [f"- {m}: {l[:220]}" for m, l, _ in sorted(lessons, key=lambda x: -x[2])[:12]] or ["- none written yet"]
    broken = {}
    for r in recent:
        b = (r.get("broken") or "").strip()
        if b and not _is_none(b):
            broken.setdefault(b[:120], []).append(r["member"])
    out += ["", "## Broken instruments (each one has an issue; fix or claim it, do not re-report it)"]
    open_titles = {i.get("title", ""): i.get("url", "") for i in instrument_issues}
    for b, members in broken.items():
        url = next((u for t, u in open_titles.items() if b[:60] in t), "")
        out.append(f"- {b} -- seen by {', '.join(sorted(set(members)))}" + (f" -- {url}" if url else " -- (issue pending)"))
    for t, u in open_titles.items():
        if not any(b[:60] in t for b in broken):
            out.append(f"- {t} -- {u}")
    if not broken and not open_titles:
        out.append("- none reported")
    sig = LOG_DIR / "SIGNALS.md"
    if sig.exists():
        out += ["", "## Yesterday in the product (signals)"]
        out += [ln for ln in sig.read_text(errors="ignore").splitlines()[:20] if ln.strip() and not ln.startswith("# ")]
    open_asks = [a for a in asks if a.get("status") == "open"]
    out += ["", f"## Open asks waiting on a human: {len(open_asks)}"]
    for a in sorted(open_asks, key=lambda a: a.get("filed_at", 0))[:6]:
        out.append(f"- ask #{a['id']} ({a.get('class') or 'unclassed'}, {_ago(a.get('filed_at', now), now)}, {a.get('member')}): {(a.get('why') or '').strip().splitlines()[0][:120]}")
    return "\n".join(out) + "\n"


def load_asks() -> list[dict]:
    try:
        sys.path.insert(0, str(HERE))
        import ask, fleet_db  # noqa: E402
        conn = fleet_db.connect()
        try:
            return ask.list_asks(conn, status="open", limit=100)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def instrument_issues(run=_run) -> list[dict]:
    r = run(["gh", "issue", "list", "--repo", KIT_REPO, "--state", "open", "--search", '"instrument:" in:title',
             "--limit", "50", "--json", "title,url"])
    try:
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except json.JSONDecodeError:
        return []


def file_broken(runs: list[dict], now: float, run=_run, hours: float = 24.0) -> list[str]:
    """One `instrument: <name>` issue per distinct Broken: line in the window. Idempotent by
    title against the open list. Returns URLs filed."""
    since = now - hours * 3600
    have = {i["title"] for i in instrument_issues(run=run)}
    urls, seen = [], set()
    for r in runs:
        b = (r.get("broken") or "").strip()
        if (r.get("ts") or 0) < since or not b or _is_none(b):
            continue
        title = f"instrument: {b[:100]}"
        if title in have or title in seen:
            continue
        seen.add(title)
        body = (f"`{r.get('member')}` reported this instrument broken in run `{r.get('run_id')}`:\n\n> {b}\n\n"
                f"Outcome of that run: {r.get('outcome') or '(none)'}\n\n"
                "This issue is the owner. Fix the instrument here (fleet-kit), or if it needs a human "
                "(a token, an account), say exactly what and file an ask. Close when a member's next "
                "run no longer reports it broken.\n\n<!-- handoff:instrument -->")
        res = run(["gh", "issue", "create", "--repo", KIT_REPO, "--title", title, "--label", "priority-high",
                   "--body", body])
        if res.returncode == 0 and res.stdout.strip():
            urls.append(res.stdout.strip().splitlines()[-1])
    return urls


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write"); w.add_argument("--hours", type=float, default=24.0); w.add_argument("--no-gh", action="store_true")
    sub.add_parser("file-broken")
    a = ap.parse_args(argv)
    now = time.time()
    runs = load_runs()
    if a.cmd == "write":
        issues = [] if a.no_gh else instrument_issues()
        text = render(runs, load_asks(), issues, now, a.hours)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        HANDOFF.write_text(text)
        print(f"wrote {HANDOFF} ({len(text)} chars)")
        return 0
    urls = file_broken(runs, now)
    print("\n".join(urls) if urls else "nothing new to file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
