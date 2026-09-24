#!/usr/bin/env python3
"""coverage_map -- which production systems exist, and which fleet member watches each?

WHY (Reif, 2026-09-24): "it's me having to find all of these things." The product repo's devops
lane (atlas-serve uptime/Postgres/deploy/cost) went dead when the dino fleet replaced the in-repo
lanes, and for weeks no member watched the prod database: 157/200 connections, no
statement_timeout, a 178s query, silently failing ingests -- all found by a human. The fleet had
no way to notice that a system had NO watcher, because nothing listed the systems.

WHAT IT DOES (daily, librarian's Part B):
  1. Checklist: docs/prod_readiness.json -- the standard production-readiness items every web
     app shares (golden signals, DB, server, CDN, jobs, deploys, security, email, analytics,
     payments, costs). Not discovered from scratch; seeded from SRE practice.
  2. Inventory: the systems that really exist, read live -- the prod box and its DB and crontab
     (the prod-runtime probe capture), DNS/CDN/mail from public DNS, payments/analytics from the
     product repo. An item whose system is absent scores n/a; a system of a kind no checklist
     item covers scores unwatched ("unclassified").
  3. Watchers: each member's fleet.json `mandate.watches` (checklist ids). A watch only counts
     if the member is live (enabled or dispatched on demand) AND has run in the last
     WATCH_FRESH_S -- a mandate on a member that stopped running watches nothing.
  4. Retired lanes: docs/retired_lanes.json. An item a retired lane used to watch that nobody
     watches now is flagged as "a lane that died in a migration".
  5. Diff vs yesterday's map, one issue per area with unwatched items (deduped through
     issue_cluster), and one line to Reif HQ: what changed, or "all watched".

Usage:
  coverage_map.py                       # score from repo files + live inventory, print
  coverage_map.py --file --push         # + issues + HQ line (librarian's daily form)
  coverage_map.py --no-live             # score with no inventory (every item applicable)
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
WATCH_FRESH_S = 3 * 86400
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
          now: float | None = None) -> dict:
    """Pure: -> {"items": [...], "systems": [...], "retired_gaps": [...]}."""
    now = now or time.time()
    live = {}
    for m in members:
        if not (m.get("enabled") or m.get("name") in ON_DEMAND):
            continue
        name = m.get("name", "?")
        last = (last_runs or {}).get(name)
        if last_runs is not None and (last is None or now - last > WATCH_FRESH_S):
            continue  # a watcher that is not running watches nothing
        for wid in (m.get("mandate") or {}).get("watches") or []:
            live.setdefault(wid, []).append(name)
    kinds = None if inventory is None else {s["kind"] for s in inventory}

    items = []
    for it in checklist:
        by = sorted(live.get(it["id"], []))
        if kinds is not None and it.get("needs") and it["needs"] not in kinds:
            status, ev = "n/a", f"no {it['needs']} system in live inventory"
        elif by:
            status = "watched"
            ev = ", ".join(f"{b} ({_ago(last_runs.get(b), now)})" if last_runs else b for b in by)
        else:
            status, ev = "unwatched", "no live member's mandate.watches names this"
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
            for r in retired for wid in r.get("watched", []) if status.get(wid) == "unwatched"]
    return {"items": items, "systems": systems, "retired_gaps": gaps}


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


def hq_line(prev: dict | None, cur: dict) -> str:
    unw = [it["id"] for it in cur["items"] if it["status"] == "unwatched"]
    unsys = [s["id"] for s in cur["systems"] if s["status"] == "unwatched"]
    changes = diff(prev, cur)
    if not changes and not unw and not unsys:
        return "coverage map: all watched"
    head = "coverage map changed: " + "; ".join(changes[:10]) if changes else "coverage map: no change since yesterday"
    tail = f" | unwatched ({len(unw)}): {', '.join(unw)}" if unw else " | all checklist items watched"
    if unsys:
        tail += f" | unwatched systems: {', '.join(unsys[:10])}"
    return head + tail


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


def live_inventory(repo_dir: str, domain: str) -> list[dict]:
    inv: list[dict] = []
    import prod_runtime
    cap = LOG_DIR / "prod_runtime.last.txt"
    raw = cap.read_text() if cap.exists() and time.time() - cap.stat().st_mtime < 86400 else prod_runtime.fetch()
    p = prod_runtime.parse(raw)
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
        if it["status"] == "unwatched":
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
                "exist on the live system but no live fleet member's `mandate.watches` names them, so "
                "nothing would notice if they broke.\n\n"
                + "\n".join(f"- [ ] `{it['id']}` {it['item']}"
                            + (f" -- **died in migration**: was watched by retired lane "
                               f"{', '.join(died[it['id']])}" if it["id"] in died else "")
                            for it in its)
                + "\n\nDone = an existing member's charter + `mandate.watches` covers each item (no new "
                  "members: fold it into the closest lane), and the next daily map scores it watched.")
        out.append(issue_cluster.file_issue(f"blind spot: {area} watched by no fleet member", body,
                                            ["fleet:backlog", LANE_LABEL, issue_cluster.MEGA_LABEL],
                                            repo=repo, who="librarian", open_issues=open_issues)
                   | {"area": area})
    return out


def push_line(line: str, now: float | None = None) -> dict:
    now = int(now or time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    rec = {"received_at": now, "caller": "coverage-map", "host": "fleet coverage map",
           "signature": f"coverage:{day}", "transition": "info", "start_ts": now, "observed_ts": now,
           "title": "daily coverage map", "detail": line[:500],
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
    a = ap.parse_args(argv)

    checklist = json.loads(CHECKLIST.read_text())["items"]
    retired = json.loads(RETIRED.read_text())["lanes"]
    inv = None if a.no_live else live_inventory(a.repo_dir, a.domain)
    m = score(checklist, load_members(ROOT / "members"), retired, inv, last_runs_from_db())
    prev_path = LOG_DIR / "coverage_map.json"
    prev = json.loads(prev_path.read_text()) if prev_path.exists() else None
    print(render(m))
    line = hq_line(prev, m)
    print("\n" + line)
    if a.file:
        if Path(a.repo_dir).is_dir():
            os.chdir(a.repo_dir)
        for r in file_gaps(m):
            print(f"  filed {r['area']}: {r.get('action')} {r.get('number') or r.get('url') or r.get('error', '')}")
    if a.push:
        print(f"  hq push: {push_line(line)}")
    if LOG_DIR.is_dir():
        prev_path.write_text(json.dumps(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
