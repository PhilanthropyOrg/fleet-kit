#!/usr/bin/env python3
"""inflows -- is every source of data coming INTO the business actually arriving?

WHY (Reif, 2026-09-24): "I'm especially interested in data inflows; there can't be that many:
server, database, Google Analytics..." The app's own source pool read 4 STALE / 5 NO_CREDENTIAL
that day -- and the 5 NO_CREDENTIAL were false: the keys were on the box, the prober's cron env
just lacked the endpoint base. Meanwhile Cloudflare's analytics API had been sunset and the news,
social and website-content tables had stopped growing six weeks earlier. Nobody had one list.

docs/data_inflows.json is that list. score() turns one prod probe capture
(scripts/prod_runtime_probe.sh) into a status per source with a MEASURED last success:
  cron   -> the job's last `EXIT 0` in its /var/log/atlas log (cron_exits section)
  signal -> a live 200 from the app's own /990/api/signals/<name> read endpoint (signals)
  event  -> the newest row the source wrote (inflow_ts)
and credential presence from env key NAMES on the box (envkeys; never a value).

Run by librarian's daily audit (scripts/coverage_map.py); the table is the first section of its
HQ line. Any non-OK source fails that audit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "data_inflows.json"
ISSUE_STATES = ("DOWN", "NO_CREDENTIAL")


def load() -> list[dict]:
    return json.loads(REGISTRY.read_text())["sources"]


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score(sources: list[dict], p: dict, now: float | None = None) -> list[dict]:
    """Pure: sources x parsed probe capture -> one row per source."""
    now = now or time.time()
    taken = next((_num(r[1]) for r in p.get("meta", []) if len(r) > 1 and r[0] == "now"), None) or now
    exits = {r[0]: r for r in p.get("cron_exits", []) if len(r) >= 5}
    installed = {r[0] for r in p.get("crontab", []) if r and r[0]}
    signals = {r[0]: r for r in p.get("signals", []) if len(r) >= 2}
    events = {r[0]: (r[1] if len(r) > 1 else "") for r in p.get("inflow_ts", []) if r and r[0]}
    keys = {r[0] for r in p.get("envkeys", []) if r and r[0]} if "envkeys" in p else None

    rows = []
    for s in sources:
        last_ok, notes, measured, failing = None, [], False, False
        for m in s.get("measure", []):
            if "cron" in m:
                n = m["cron"]
                if n not in installed:
                    notes.append(f"cron {n} not installed")
                e = exits.get(n)
                if e:
                    measured = True
                    ok = _num(e[4]) or None
                    if e[2] != "0":
                        notes.append(f"{n} last rc={e[2]}")
                    last_ok = max(filter(None, [last_ok, ok]), default=None)
                elif "cron_exits" in p:
                    measured = True
                    notes.append(f"{n}: no run log in 40 days")
            elif "signal" in m:
                r = signals.get(m["signal"])
                if r:
                    measured = True
                    if r[1] == "200" and (len(r) < 3 or r[2].strip().startswith("ok")):
                        last_ok = taken  # a live 200 at capture time
                    else:
                        failing = True
                        notes.append(f"/signals/{m['signal']} HTTP {r[1]}: {' '.join((r[2] if len(r) > 2 else '').split())[:140]}")
            elif "event" in m:
                if m["event"] in events:
                    measured = True
                    ts = _num(events[m["event"]])
                    if ts:
                        last_ok = max(filter(None, [last_ok, ts]), default=None)
                    else:
                        notes.append(f"{m['event']}: no rows ever")
        missing = [k for k in s.get("creds", []) if keys is not None and k not in keys]
        if missing:
            status = "NO_CREDENTIAL"
            notes.insert(0, "not set on the box: " + ", ".join(missing))
        elif not measured:
            status = "NEVER_CHECKED"
        elif failing or not last_ok:
            status = "DOWN"
        elif now - last_ok > s["fresh_h"] * 3600:
            status = "STALE"
        else:
            status = "OK"
        rows.append({**s, "status": status, "last_ok": last_ok, "creds_ok": not missing,
                     "note": "; ".join(notes)})
    return rows


def _age(t: float | None, now: float) -> str:
    if not t:
        return "never"
    h = (now - t) / 3600
    return f"{h * 60:.0f}m ago" if h < 1 else f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f}d ago"


def render(rows: list[dict], now: float | None = None) -> str:
    now = now or time.time()
    ok = sum(1 for r in rows if r["status"] == "OK")
    out = [f"DATA INFLOWS: {ok}/{len(rows)} OK",
           f"  {'source':<26} {'status':<13} {'last success':<13} {'cred':<4} {'fresh':<6} {'owner':<9} feeds / note"]
    for r in sorted(rows, key=lambda r: (r["status"] == "OK", r["status"], r["id"])):
        out.append(f"  {r['id']:<26} {r['status']:<13} {_age(r['last_ok'], now):<13} "
                   f"{'yes' if r['creds_ok'] else 'NO':<4} {str(r['fresh_h']) + 'h':<6} {r['owner']:<9} "
                   f"{r['feeds']}" + (f" | {r['note']}" if r["note"] else ""))
    reif = [r for r in rows if r["status"] != "OK" and r.get("reif_only")]
    if reif:
        out.append("  NEEDS REIF: " + "; ".join(f"{r['id']}: {r['need']}" for r in reif))
    return "\n".join(out)


def hq_section(rows: list[dict], now: float | None = None) -> str:
    """The first section of librarian's daily HQ line: compact, every non-OK source named."""
    now = now or time.time()
    bad = [r for r in rows if r["status"] != "OK"]
    head = f"inflows {len(rows) - len(bad)}/{len(rows)} OK"
    by: dict[str, list[str]] = {}
    for r in bad:
        by.setdefault(r["status"], []).append(r["id"] + (f" {_age(r['last_ok'], now)}" if r["status"] == "STALE" else ""))
    return head + "".join(f" | {k}: {', '.join(v)}" for k, v in sorted(by.items()))


def file_issues(rows: list[dict], repo: str | None = None) -> list[dict]:
    """One deduped issue per DOWN / NO_CREDENTIAL source saying exactly what's needed, plus ONE
    'needs Reif' issue listing every credential only Reif can supply."""
    import issue_cluster
    todo = [r for r in rows if r["status"] in ISSUE_STATES and not r.get("reif_only")]
    reif = [r for r in rows if r["status"] != "OK" and r.get("reif_only")]
    if not todo and not reif:
        return []
    try:
        open_issues = issue_cluster.list_open(repo)
    except (RuntimeError, ValueError):
        open_issues = None
    labels = ["fleet:backlog", "lane:prod-runtime"]
    out = []
    for r in todo:
        body = (f"Daily data-inflows audit (`scripts/coverage_map.py`, librarian; registry "
                f"`docs/data_inflows.json`): **{r['name']}** is **{r['status']}**.\n\n"
                f"- feeds: {r['feeds']}\n- owner: `{r['owner']}`\n- evidence: {r['note'] or '-'}\n\n"
                f"**Needed:** {r['need']}\n\nDone = the next daily audit scores `{r['id']}` OK.")
        out.append(issue_cluster.file_issue(f"data inflow {r['id']} is {r['status']}", body, labels,
                                            repo=repo, who="librarian", open_issues=open_issues)
                   | {"id": r["id"]})
    if reif:
        body = ("Credentials or accounts only Reif can supply. Each unblocks one data inflow in "
                "`docs/data_inflows.json`; librarian's daily audit fails until it is set or Reif "
                "retires the row.\n\n"
                + "\n".join(f"- [ ] **{r['name']}** (`{r['id']}`, {r['status']}): {r['need']}" for r in reif))
        out.append(issue_cluster.file_issue("needs Reif: credentials for data inflows", body,
                                            labels, repo=repo, who="librarian",
                                            open_issues=open_issues) | {"id": "needs-reif"})
    return out
