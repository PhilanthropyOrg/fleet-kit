#!/usr/bin/env python3
"""coverage_map -- which production systems exist, and which fleet member watches each?

WHY (Reif, 2026-09-24): "it's me having to find all of these things." The product repo's devops
lane (atlas-serve uptime/Postgres/deploy/cost) went dead when the dino fleet replaced the in-repo
lanes, and for weeks no member watched the prod database: 157/200 connections, no
statement_timeout, a 178s query, silently failing ingests -- all found by a human. The fleet had
no way to notice that a system had NO watcher, because nothing listed the systems.

WHAT IT DOES (daily, librarian's Part B -- the production-readiness audit):
  1. Checklist: docs/prod_readiness.json -- the standard production-readiness items every web
     app shares, PERMANENT and 100% OWNED: each item names one existing member (`owner`), the
     `check` it runs, and `cadence_h`. An item with no live owner scores `unowned`; one whose
     owner's check has not run within cadence_h (the mtime of its `evidence` file, else the
     owner's last run in fleet.db) scores `overdue`. Both FAIL the audit.
  2. Data inflows: docs/data_inflows.json scored by scripts/inflows.py from the same prod probe
     capture -- last success measured per source. Any non-OK source FAILS the audit; DOWN and
     NO_CREDENTIAL sources become one issue each, credentials only Reif can supply one list.
  3. Inventory: the systems that really exist, read live -- the prod box and its DB and crontab
     (the prod-runtime probe capture), DNS/CDN/mail from public DNS, payments/analytics from the
     product repo. An owned item whose system is absent scores n/a; a system of a kind no
     checklist item covers scores unwatched ("unclassified").
  4. Retired lanes: docs/retired_lanes.json. An item a retired lane used to watch that is now
     unowned/overdue is flagged as "a lane that died in a migration".
  5. Diff vs yesterday's map, one issue per area with failing items (deduped through
     issue_cluster), and one line to Reif HQ: the inflows table first, then the checklist.
     Exit 1 when the audit fails, so the pass reads as failed, not green.

Usage:
  coverage_map.py                       # score from repo files + live inventory, print
  coverage_map.py --file --push         # + issues + HQ line (librarian's daily form)
  coverage_map.py --no-live             # score with no inventory (every item applicable)
  coverage_map.py --capture FILE        # score a saved prod probe capture
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))
CHECKLIST = ROOT / "docs" / "prod_readiness.json"
RETIRED = ROOT / "docs" / "retired_lanes.json"
# Dispatch-only members: enabled:false in their spec, but gru/vp_due spawn them every hour.
ON_DEMAND = {"nerd", "minion", "vp"}
FAILING = ("unowned", "overdue")
LANE_LABEL = "lane:prod-runtime"


def load_members(members_dir: Path) -> list[dict]:
    out = []
    for p in sorted(members_dir.glob("*/*.fleet.json")):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def score(checklist: list[dict], members: list[dict], retired: list[dict],
          inventory: list[dict] | None, last_runs: dict[str, float] | None,
          now: float | None = None, evidence_mtimes: dict[str, float] | None = None) -> dict:
    """Pure: -> {"items": [...], "systems": [...], "retired_gaps": [...]}.

    Every item must name one live `owner` whose check ran within `cadence_h`: last run is the
    mtime of the item's `evidence` file when it has one, else the owner's last run in fleet.db.
    last_runs None = run history unreadable (CI, a laptop): ownership is still enforced."""
    now = now or time.time()
    by_name = {m.get("name"): m for m in members}
    items = []
    for it in checklist:
        owner = it.get("owner")
        m = by_name.get(owner)
        by = [owner] if owner else []
        if not owner or m is None:
            status, ev = "unowned", f"owner {owner!r} is not a fleet member" if owner else "no owner"
        elif not (m.get("enabled") or owner in ON_DEMAND):
            status, ev = "unowned", f"owner {owner} is disabled"
        else:
            last = (evidence_mtimes or {}).get(it.get("evidence")) or (last_runs or {}).get(owner)
            src = it.get("evidence") or "run"
            if last_runs is None and not evidence_mtimes:
                status, ev = "watched", f"{owner} (run history unavailable)"
            elif not last or now - last > it.get("cadence_h", 72) * 3600:
                status = "overdue"
                ev = f"{owner}'s check last ran {_ago(last, now).replace('ran ', '')} ({src}), cadence {it.get('cadence_h', 72)}h"
            else:
                status, ev = "watched", f"{owner} ({_ago(last, now)}, {src})"
        if status == "watched" and inventory is not None and it.get("needs") \
                and it["needs"] not in {s["kind"] for s in inventory}:
            status, ev = "n/a", f"no {it['needs']} system in live inventory (owner {owner})"
        items.append({**it, "status": status, "by": by, "evidence": ev})

    by_kind: dict[str, set[str]] = {}
    for it in items:
        if it["status"] == "watched":
            by_kind.setdefault(it.get("needs", ""), set()).update(it["by"])
    covered_kinds = {it.get("needs") for it in checklist}
    systems = []
    for s in inventory or []:
        if s["kind"] not in covered_kinds:
            systems.append({**s, "status": "unwatched", "by": [], "evidence": "unclassified: no checklist item covers this kind"})
        else:
            by = sorted(by_kind.get(s["kind"], set()))
            systems.append({**s, "status": "watched" if by else "unwatched", "by": by,
                            "evidence": ", ".join(by) or f"no {s['kind']} checklist item is watched"})

    status = {it["id"]: it["status"] for it in items}
    gaps = [{"lane": r["lane"], "id": wid, "source": r.get("source", "")}
            for r in retired for wid in r.get("watched", []) if status.get(wid) in FAILING]
    return {"items": items, "systems": systems, "retired_gaps": gaps}


def failed(m: dict, inflow_rows: list[dict] | None = None) -> bool:
    """The audit's verdict: any unowned/overdue item or any non-OK inflow fails it."""
    return any(it["status"] in FAILING for it in m["items"]) or \
        any(r["status"] != "OK" for r in inflow_rows or [])


def _ago(t: float | None, now: float) -> str:
    if not t:
        return "never ran"
    h = (now - t) / 3600
    return f"ran {h:.0f}h ago" if h < 48 else f"ran {h / 24:.0f}d ago"


def diff(prev: dict | None, cur: dict) -> list[str]:
    if not prev:
        return ["first map: " + _counts(cur)]
    old = {it["id"]: it["status"] for it in prev.get("items", [])}
    out = [f"{it['id']}: {old.get(it['id'], 'new')} -> {it['status']}"
           for it in cur["items"] if old.get(it["id"]) != it["status"]]
    olds = {s["id"] for s in prev.get("systems", [])}
    out += [f"new system {s['id']} ({s['status']})" for s in cur["systems"] if s["id"] not in olds]
    return out


def _counts(m: dict) -> str:
    c = {}
    for it in m["items"]:
        c[it["status"]] = c.get(it["status"], 0) + 1
    return ", ".join(f"{v} {k}" for k, v in sorted(c.items()))


def hq_line(prev: dict | None, cur: dict, inflow_rows: list[dict] | None = None) -> str:
    """Inflows first (Reif's ask), then checklist ownership. Leads with AUDIT FAIL when failing."""
    import inflows
    first = inflows.hq_section(inflow_rows) + " || " if inflow_rows else ""
    verdict = "AUDIT FAIL: " if failed(cur, inflow_rows) else ""
    bad = [f"{it['id']} {it['status']}" for it in cur["items"] if it["status"] in FAILING]
    unsys = [s["id"] for s in cur["systems"] if s["status"] == "unwatched"]
    changes = diff(prev, cur)
    if not changes and not bad and not unsys:
        return verdict + first + f"checklist: all {len(cur['items'])} owned and fresh"
    head = "checklist changed: " + "; ".join(changes[:10]) if changes else "checklist: no change since yesterday"
    tail = f" | unowned/overdue ({len(bad)}): {', '.join(bad)}" if bad else \
        f" | all {len(cur['items'])} items owned and fresh"
    if unsys:
        tail += f" | unwatched systems: {', '.join(unsys[:10])}"
    return verdict + first + head + tail


def render(m: dict) -> str:
    lines = [f"coverage map: {_counts(m)}"]
    area = None
    for it in m["items"]:
        if it["area"] != area:
            area = it["area"]
            lines.append(f"  {area}")
        lines.append(f"    {it['status']:<9} {it['id']:<22} {it['evidence']}")
    for g in m["retired_gaps"]:
        lines.append(f"  DIED IN MIGRATION: {g['id']} was watched by retired lane '{g['lane']}', nobody now")
    for s in m["systems"]:
        if s["status"] != "watched":
            lines.append(f"  unwatched system: {s['id']} ({s['evidence']})")
    return "\n".join(lines)


# ---- live inventory (thin seams) ---------------------------------------------------------

def _doh(name: str, rtype: str) -> list[str]:
    req = urllib.request.Request(f"https://cloudflare-dns.com/dns-query?name={name}&type={rtype}",
                                 headers={"accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return [a.get("data", "") for a in json.load(r).get("Answer", [])]
    except Exception:  # noqa: BLE001 -- inventory is best-effort; a miss shows as n/a, never a crash
        return []


def capture(path: str | None = None) -> dict:
    """The parsed prod probe capture: nerd's 3-hourly one if under 6h old, else a fresh probe."""
    import prod_runtime
    cap = Path(path) if path else LOG_DIR / "prod_runtime.last.txt"
    fresh = path or (cap.exists() and time.time() - cap.stat().st_mtime < 6 * 3600)
    return prod_runtime.parse(cap.read_text() if fresh else prod_runtime.fetch())


def live_inventory(repo_dir: str, domain: str, p: dict) -> list[dict]:
    inv: list[dict] = []
    import prod_runtime
    host = prod_runtime._kv(p.get("meta", [])).get("host")
    if host:
        inv.append({"id": f"host:{host}", "kind": "host"})
    if p.get("pg_settings") and not any(r and r[0] == "error" for r in p["pg_settings"]):
        inv.append({"id": "db:postgres", "kind": "db"})
    inv += [{"id": f"cron:{r[0]}", "kind": "cron"} for r in p.get("crontab", []) if r and r[0]]
    if domain:
        inv.append({"id": f"web:{domain}", "kind": "web"})
        inv.append({"id": f"dns:{domain}", "kind": "dns"})
        if any("cloudflare" in ns.lower() for ns in _doh(domain, "NS")):
            inv.append({"id": "cdn:cloudflare", "kind": "cdn"})
        if _doh(domain, "MX") or any("spf1" in t for t in _doh(domain, "TXT")):
            inv.append({"id": f"email:{domain}", "kind": "email"})
    for kind, pattern in (("payments", "stripe"), ("analytics", "posthog|gtag|googletagmanager")):
        r = subprocess.run(["git", "-C", repo_dir, "grep", "-l", "-i", "-E", pattern, "--", "src"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            inv.append({"id": f"{kind}:{pattern.split('|')[0]}", "kind": kind})
    return inv


def last_runs_from_db() -> dict[str, float] | None:
    db = LOG_DIR / "fleet.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)  # members write it all day
    try:
        rows = con.execute("SELECT member, MAX(recorded_at) FROM runs GROUP BY member").fetchall()
    finally:
        con.close()
    out = {}
    for member, ts in rows:
        try:
            out[member] = float(ts)
        except (TypeError, ValueError):
            try:
                out[member] = time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                pass
    return out


def file_gaps(m: dict, repo: str | None = None) -> list[dict]:
    import issue_cluster
    by_area: dict[str, list[dict]] = {}
    for it in m["items"]:
        if it["status"] in FAILING:
            by_area.setdefault(it["area"], []).append(it)
    died = {}
    for g in m["retired_gaps"]:
        died.setdefault(g["id"], []).append(g["lane"])
    out = []
    try:  # one board read per run, not one per issue; on failure file_issue retries and reports it
        open_issues = issue_cluster.list_open(repo) if by_area else []
    except (RuntimeError, ValueError):
        open_issues = None
    for area, its in by_area.items():
        body = (f"Daily blind-spot audit (`scripts/coverage_map.py`, librarian): these **{area}** checks "
                "have no live owner, or their owner's check has not run within its cadence, so "
                "nothing would notice if they broke.\n\n"
                + "\n".join(f"- [ ] `{it['id']}` {it['item']} -- {it['status']}: {it['evidence']}"
                            + (f" -- **died in migration**: was watched by retired lane "
                               f"{', '.join(died[it['id']])}" if it["id"] in died else "")
                            for it in its)
                + "\n\nDone = the item's `owner` in docs/prod_readiness.json is an existing live member whose "
                  "charter runs its `check` (no new members: fold it into the closest lane), and the next "
                  "daily audit scores it watched.")
        out.append(issue_cluster.file_issue(f"blind spot: {area} checks unowned or overdue", body,
                                            ["fleet:backlog", LANE_LABEL, issue_cluster.MEGA_LABEL],
                                            repo=repo, who="librarian", open_issues=open_issues)
                   | {"area": area})
    return out


def push_line(line: str, now: float | None = None) -> dict:
    now = int(now or time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    rec = {"received_at": now, "caller": "coverage-map", "host": "fleet coverage map",
           "signature": f"coverage:{day}", "transition": "info", "start_ts": now, "observed_ts": now,
           "title": "daily prod-readiness audit", "detail": line[:500],
           "idempotency_key": hashlib.sha256(f"coverage:{day}".encode()).hexdigest()[:32]}
    path = LOG_DIR / "prod-alerts.jsonl"
    if path.exists() and rec["idempotency_key"] in path.read_text():
        return {"sent": False, "reason": "already pushed today"}
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return {"sent": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--file", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--repo-dir", default=os.environ.get("FLEET_REPO", "/repo"))
    ap.add_argument("--domain", default=os.environ.get("FLEET_PROD_DOMAIN", ""))
    ap.add_argument("--capture", help="score a saved prod probe capture instead of the live one")
    a = ap.parse_args(argv)
    import inflows

    checklist = json.loads(CHECKLIST.read_text())["items"]
    retired = json.loads(RETIRED.read_text())["lanes"]
    p = None if a.no_live else capture(a.capture)
    inv = None if p is None else live_inventory(a.repo_dir, a.domain, p)
    ev = {it["evidence"]: (LOG_DIR / it["evidence"]).stat().st_mtime for it in checklist
          if it.get("evidence") and (LOG_DIR / it["evidence"]).exists()}
    m = score(checklist, load_members(ROOT / "members"), retired, inv, last_runs_from_db(), evidence_mtimes=ev)
    rows = None if p is None else inflows.score(inflows.load(), p)
    prev_path = LOG_DIR / "coverage_map.json"
    prev = json.loads(prev_path.read_text()) if prev_path.exists() else None
    if rows is not None:
        print(inflows.render(rows) + "\n")
    print(render(m))
    line = hq_line(prev, m, rows)
    print("\n" + line)
    if a.file:
        if Path(a.repo_dir).is_dir():
            os.chdir(a.repo_dir)
        for r in file_gaps(m):
            print(f"  filed {r['area']}: {r.get('action')} {r.get('number') or r.get('url') or r.get('error', '')}")
        for r in inflows.file_issues(rows or []):
            print(f"  filed inflow {r['id']}: {r.get('action')} {r.get('number') or r.get('url') or r.get('error', '')}")
    if a.push:
        print(f"  hq push: {push_line(line)}")
    if LOG_DIR.is_dir():
        prev_path.write_text(json.dumps(m))
    if failed(m, rows):
        print("AUDIT FAILED: an unowned/overdue checklist item or a non-OK data inflow (see above)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
