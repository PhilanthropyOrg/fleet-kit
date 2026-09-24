#!/usr/bin/env python3
"""prod_runtime -- read the LIVE prod system (not the repo) and file what is wrong with it.

WHY (Reif, 2026-09-24): "fleet-kit is supposed to be self-improving and self-learning. How is it
not thinking that we need to check the database?" The product repo's old `devops` lane
(atlas-serve prod system: uptime/Postgres/deploy delivery/cost) died when the dino fleet replaced
the in-repo lanes, and no fleet member inherited prod runtime health. Found by hand the same day:
157/200 DB connections (141 idle from the app box), the app running as doadmin, officers ILIKE at
295ms mean / 15s max over 1.2M calls, a 178s query with no statement_timeout, a full-scan vector
search at 6.3M calls, ingest writing 14.5M rows into the live primary, and ingest crons failing
silently. Every one of those is readable in seconds from the live system; nobody was reading it.

OWNER: nerd's `prod-runtime` lane (members/nerd/nerd.md). This script is the deterministic half
-- zero LLM, runs on its own cron tick so a breach pages even when gru never dispatches the lane.
The nerd pass is the exploratory half: it runs this, then digs into what the checks do not cover.

ACCESS: one forced-command SSH key (FLEET_PROD_PROBE_KEY) that can run only
scripts/prod_runtime_probe.sh on the prod box, which is read-only by construction (see its
header). The DB credential never leaves the prod box.

OUTPUT: one issue per failing check through issue_cluster.file_issue (dedupe at birth: a check
still failing next tick comments on its own issue, never a twin), labelled fleet:mega so it is
one batch item with all its evidence. A BREACH also goes to Reif HQ through the existing push
channel (logs/prod-alerts.jsonl -> notify_hq_prod_alert.py -> job.sh), START once and RESOLVE
once per episode, never every tick.

Usage:
  prod_runtime.py                 # probe live, print summary
  prod_runtime.py --file --push   # + file issues, + HQ push for breaches (the cron form)
  prod_runtime.py --from FILE     # parse a saved probe capture instead of probing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LANE = "prod-runtime"
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))

# Thresholds. A BREACH pages HQ; a FINDING is filed and waits for the board.
CONN_BREACH_PCT = 0.75        # connections in use / max_connections
CONN_IDLE_SHARE = 0.6         # idle share of in-use connections that says "pool oversized"
HOT_MEAN_MS = 250             # a statement this slow on average ...
HOT_MIN_CALLS = 10_000        # ... at this volume is a real user-facing cost
MAX_MS_BREACH = 60_000        # any statement that ever ran this long
LONG_TX_BREACH_S = 300        # an open transaction/query this old right now
SEQSCAN_MIN_ROWS = 1_000_000  # table size worth an index-miss look
HEAVY_WRITES = 10_000_000     # rows written to one table since stats reset
LOAD_PER_CPU_BREACH = 1.5
MEM_AVAIL_BREACH = 0.10
DISK_BREACH_PCT = 85
JOURNAL_ERR_PER_H = 100
INGEST_MAX_AGE_S = 35 * 86400  # the IRS/BMF ingests are monthly
INGEST_NAMES = ("bmf_refresh", "ingest_irs_xml", "determination_letters", "ingest_endowment")


def parse(text: str) -> dict[str, list[list[str]]]:
    """`@@section` headers, each followed by tab-separated rows -> {section: [[field, ...]]}."""
    out: dict[str, list[list[str]]] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].split()[0] if line[2:].strip() else None
            if cur:
                out.setdefault(cur, [])
            continue
        if cur is None or not line.strip():
            continue
        out[cur].append(line.split("\t"))
    return out


def _kv(rows: list[list[str]]) -> dict[str, str]:
    return {r[0]: r[1] for r in rows if len(r) >= 2}


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def statements(rows: list[list[str]]) -> list[dict]:
    """pg_stat_statements rows: calls, total_ms, mean_ms, max_ms, rows, query."""
    out = []
    for r in rows:
        if len(r) < 6 or r[0] == "error":
            continue
        out.append({"calls": int(_num(r[0])), "total_ms": _num(r[1]), "mean_ms": _num(r[2]),
                    "max_ms": _num(r[3]), "rows": int(_num(r[4])), "query": "\t".join(r[5:])})
    return out


def _finding(check, severity, title, evidence, fix=""):
    return {"check": check, "severity": severity, "title": title, "evidence": evidence, "fix": fix}


def _q(s: dict) -> str:
    return (f"{s['calls']:,} calls, mean {s['mean_ms']:.0f}ms, max {s['max_ms'] / 1000:.1f}s, "
            f"total {s['total_ms'] / 3.6e6:.1f}h: `{s['query'][:160]}`")


def evaluate(p: dict, now: float | None = None, registry: set[str] | None = None) -> list[dict]:
    """Parsed probe -> findings. Pure: no I/O, so every threshold is pinned by a fixture test."""
    now = now or time.time()
    f: list[dict] = []
    errors = [r for sec, rows in p.items() for r in rows if r and r[0] == "error"]
    if errors:
        f.append(_finding("probe-error", "breach", "probe could not read part of the live system",
                          [" ".join(r[1:])[:200] for r in errors[:5]],
                          "a blind section is a blind spot -- fix the probe or its access"))

    settings = _kv(p.get("pg_settings", []))
    max_conn = _num(settings.get("max_connections"), 0)
    conns = [(r[0], r[1], r[2], int(_num(r[3]))) for r in p.get("pg_conn", []) if len(r) >= 4]
    used = sum(c[3] for c in conns)
    idle = sum(c[3] for c in conns if c[2] == "idle")
    if max_conn and used / max_conn >= CONN_BREACH_PCT:
        f.append(_finding("db-connections", "breach",
                          f"DB connections at {used}/{int(max_conn)}",
                          [f"{u}@{a} {s}: {n}" for u, a, s, n in conns[:8]],
                          "cap the app pool; the DB runs out of slots before it runs out of CPU"))
    elif used and idle / used >= CONN_IDLE_SHARE and idle >= 20:
        f.append(_finding("db-pool-oversized", "finding",
                          f"DB pool oversized: {idle} of {used} connections idle",
                          [f"{u}@{a} {s}: {n}" for u, a, s, n in conns[:8]],
                          "shrink the app's pool (or add pgbouncer) so idle slots stop eating max_connections"))

    if settings.get("db_statement_timeout", "0") in ("0", "0ms", ""):
        roles = {r[0]: r[2] if len(r) > 2 else "" for r in p.get("pg_role_settings", []) if r}
        app_users = sorted({c[0] for c in conns if c[0] and c[0] != "postgres"})
        unguarded = [u for u in app_users if "statement_timeout" not in roles.get(u, "")]
        if unguarded:
            f.append(_finding("db-statement-timeout", "breach",
                              "no statement_timeout for the app's DB role",
                              [f"server default statement_timeout=0; role(s) with no override: "
                               f"{', '.join(unguarded)}"],
                              "ALTER ROLE <app role> SET statement_timeout = '30s' (batch jobs opt out per session)"))

    superish = {r[0] for r in p.get("pg_role_settings", []) if len(r) >= 2 and r[1] == "1"}
    app_super = sorted({c[0] for c in conns if c[0] in superish})
    if app_super:
        f.append(_finding("db-least-privilege", "finding",
                          f"app connects as a privileged DB role ({', '.join(app_super)})",
                          [f"{u} holds CREATEROLE/CREATEDB/SUPERUSER and serves app traffic" for u in app_super],
                          "create a least-privilege app role; keep the admin role for migrations"))

    tot = statements(p.get("pg_statements_total", []))
    hot = [s for s in tot if s["mean_ms"] >= HOT_MEAN_MS and s["calls"] >= HOT_MIN_CALLS]
    if hot:
        f.append(_finding("db-slow-queries", "finding",
                          f"{len(hot)} hot slow queries (mean >= {HOT_MEAN_MS}ms at >= {HOT_MIN_CALLS:,} calls)",
                          [_q(s) for s in hot[:8]],
                          "index or rewrite; ILIKE '%x%' wants a pg_trgm GIN index, vector ORDER BY wants HNSW/IVFFlat"))
    worst = statements(p.get("pg_statements_max", []))
    runaway = [s for s in worst if s["max_ms"] >= MAX_MS_BREACH]
    if runaway:
        f.append(_finding("db-runaway-queries", "breach",
                          f"{len(runaway)} statements have run >= {MAX_MS_BREACH // 1000}s",
                          [_q(s) for s in runaway[:8]],
                          "statement_timeout on the app role; move batch aggregates off the live primary"))

    long_rows = [r for r in p.get("pg_long", []) if len(r) >= 6 and r[0] != "error"]
    stuck = [r for r in long_rows if _num(r[3]) >= LONG_TX_BREACH_S or r[2].startswith("idle in transaction")]
    if stuck:
        f.append(_finding("db-long-transactions", "breach" if any(_num(r[3]) >= LONG_TX_BREACH_S for r in stuck) else "finding",
                          f"{len(stuck)} long-running or idle-in-transaction sessions",
                          [f"pid {r[0]} {r[1]} {r[2]} {r[3]}s wait={r[4] or '-'}: `{r[5][:140]}`" for r in stuck[:8]],
                          "idle_in_transaction_session_timeout + statement_timeout"))

    waiting = int(_num((p.get("pg_locks") or [["0"]])[0][0]))
    if waiting:
        f.append(_finding("db-locks", "breach" if waiting >= 5 else "finding",
                          f"{waiting} lock waits right now", [f"pg_locks not granted: {waiting}"]))

    seq = [r for r in p.get("pg_seqscan", []) if len(r) >= 5 and r[0] != "error"]
    miss = [r for r in seq if _num(r[4]) >= SEQSCAN_MIN_ROWS and _num(r[2]) / max(_num(r[4]), 1) >= 1000]
    if miss:
        f.append(_finding("db-index-miss", "finding",
                          f"{len(miss)} big tables read by sequential scan",
                          [f"{r[0]}: {int(_num(r[1])):,} seq scans read {int(_num(r[2])):,} rows "
                           f"(table {int(_num(r[4])):,} rows, {int(_num(r[3])):,} idx scans)" for r in miss[:8]],
                          "find the query behind each (pg_stat_statements) and index it"))

    heavy = [r for r in p.get("pg_writes", []) if len(r) >= 4 and r[0] != "error"
             and sum(_num(v) for v in r[1:4]) >= HEAVY_WRITES]
    if heavy:
        f.append(_finding("db-heavy-writes", "finding", f"{len(heavy)} tables take bulk writes on the live primary",
                          [f"{r[0]}: {int(_num(r[1])):,} ins / {int(_num(r[2])):,} upd / {int(_num(r[3])):,} del "
                           "since stats reset" for r in heavy[:6]],
                          "batch ingest/rebuilds off-peak or on a replica, then swap; never compete with page reads"))

    box = _kv(p.get("box", []))
    nproc = max(_num(box.get("nproc"), 1), 1)
    load5 = _num(box.get("load5"))
    mem_t, mem_a = _num(box.get("mem_total_kb")), _num(box.get("mem_avail_kb"))
    disk = _num(box.get("disk_used_pct"))
    ev = [f"load5 {load5} on {int(nproc)} cpus, mem avail {mem_a / max(mem_t, 1):.0%}, disk {disk:.0f}%"]
    if load5 / nproc >= LOAD_PER_CPU_BREACH or (mem_t and mem_a / mem_t <= MEM_AVAIL_BREACH) or disk >= DISK_BREACH_PCT:
        f.append(_finding("box-saturation", "breach", "prod box saturated", ev))

    jerr = sum(int(_num(r[1])) for r in p.get("journal", []) if len(r) >= 2)
    if jerr >= JOURNAL_ERR_PER_H:
        f.append(_finding("box-error-log", "finding", f"{jerr} error-priority journal lines in the last hour",
                          [f"{r[0]}: {r[1]}" for r in p.get("journal", [])[:8]]))
    restarts = [r for r in p.get("restarts", []) if len(r) >= 2 and _num(r[1]) > 0]
    if restarts:
        f.append(_finding("box-restarts", "finding", "app services have restarted",
                          [f"{r[0]}: NRestarts={r[1]}" for r in restarts]))

    exits = {r[0]: {"last": _num(r[1]), "rc": r[2], "fails": int(_num(r[3])), "ok": _num(r[4])}
             for r in p.get("cron_exits", []) if len(r) >= 5}
    installed = {r[0] for r in p.get("crontab", []) if r and r[0]}
    failing = sorted((n for n, e in exits.items() if e["rc"] != "0" and n in installed),
                     key=lambda n: -exits[n]["fails"])
    if failing:
        f.append(_finding("cron-failures", "finding", f"{len(failing)} installed crons failing",
                          [f"{n}: last rc={exits[n]['rc']}, {exits[n]['fails']} failed runs in 24h, "
                           f"last success {_ago(exits[n]['ok'], now)}" for n in failing[:15]],
                          "fix or delete each; a cron that fails every run and nobody reads is cost with no value"))
    if registry is not None and installed:
        drift = sorted(installed - registry), sorted(registry - installed)
        if drift[0] or drift[1]:
            f.append(_finding("cron-drift", "finding", "box crontab drifted from the repo's crontab",
                              [f"on box, not in repo: {', '.join(drift[0]) or '-'}",
                               f"in repo, not on box: {', '.join(drift[1]) or '-'}"]))

    stale = []
    for n in INGEST_NAMES:
        if n not in installed:
            continue
        e = exits.get(n)
        if not e or not e["ok"] or now - e["ok"] > INGEST_MAX_AGE_S:
            stale.append(f"{n}: last success {_ago(e['ok'] if e else 0, now)}"
                         + (f", last rc={e['rc']}" if e else ", no run log in 40 days"))
    if stale:
        f.append(_finding("ingest-freshness", "breach", f"{len(stale)} data ingests stale or failing", stale,
                          "a failing monthly ingest means pages quietly show last month's data"))
    return f


def _ago(epoch: float, now: float) -> str:
    if not epoch:
        return "never (in retained logs)"
    d = (now - epoch) / 86400
    return f"{d:.1f}d ago"


def summary(findings: list[dict], p: dict) -> str:
    box = _kv(p.get("box", []))
    conns = sum(int(_num(r[3])) for r in p.get("pg_conn", []) if len(r) >= 4)
    head = (f"prod-runtime on {_kv(p.get('meta', [])).get('host', '?')}: "
            f"{sum(1 for x in findings if x['severity'] == 'breach')} breach(es), "
            f"{sum(1 for x in findings if x['severity'] == 'finding')} finding(s); "
            f"conns {conns}/{_kv(p.get('pg_settings', [])).get('max_connections', '?')}, "
            f"load5 {box.get('load5', '?')}/{box.get('nproc', '?')}cpu, disk {box.get('disk_used_pct', '?')}%")
    lines = [head]
    for x in findings:
        lines.append(f"  [{x['severity'].upper()}] {x['check']}: {x['title']}")
        lines.extend(f"      {e}" for e in x["evidence"][:3])
    return "\n".join(lines)


def issue_title(x: dict) -> str:
    # Stable across ticks (no counts), so issue_cluster's signature dedupe keeps one issue per check.
    return f"prod-runtime: {x['check']} ({x['severity']})"


def issue_body(x: dict) -> str:
    ev = "\n".join(f"- {e}" for e in x["evidence"])
    return (f"**{x['title']}**\n\nRead from the live prod system by `scripts/prod_runtime.py` "
            f"(nerd lane `{LANE}`), {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.\n\n"
            f"Evidence:\n{ev}\n\n" + (f"Standard fix: {x['fix']}\n\n" if x["fix"] else "")
            + "Done = this check passes on the next probe run (the issue gets a comment each tick "
              "it still fails).")


def file_findings(findings: list[dict], repo: str | None = None) -> list[dict]:
    import issue_cluster
    labels = ["fleet:backlog", f"lane:{LANE}", issue_cluster.MEGA_LABEL]
    return [issue_cluster.file_issue(issue_title(x), issue_body(x), labels, repo=repo, who="prod-runtime")
            | {"check": x["check"]} for x in findings]


def push_transitions(findings: list[dict], host: str, state_path: Path, alerts_path: Path,
                     now: float | None = None) -> list[dict]:
    """START a breach once, RESOLVE it once, via the prod-alerts.jsonl HQ channel."""
    now = int(now or time.time())
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    current = {x["check"]: x for x in findings if x["severity"] == "breach"}
    records = []
    for check, x in current.items():
        if check not in state:
            state[check] = now
            records.append(("start", check, x["title"], "; ".join(x["evidence"][:3]), now))
    for check in [c for c in state if c not in current]:
        records.append(("resolve", check, f"{check} passes again", "", state.pop(check)))
    out = []
    for transition, check, title, detail, start in records:
        sig = f"{LANE}:{check}"
        key = hashlib.sha256(f"{sig}:{transition}:{start}".encode()).hexdigest()[:32]
        out.append({"received_at": now, "caller": LANE, "host": host[:100], "signature": sig,
                    "transition": transition, "start_ts": start, "observed_ts": now,
                    "title": title[:200], "detail": detail[:500], "idempotency_key": key})
    if out:
        alerts_path.parent.mkdir(parents=True, exist_ok=True)
        with alerts_path.open("a") as fh:
            for r in out:
                fh.write(json.dumps(r) + "\n")
    state_path.write_text(json.dumps(state))
    return out


def fetch() -> str:
    key = os.environ.get("FLEET_PROD_PROBE_KEY", "/fleet-kit/.prod_probe_key")
    host = os.environ.get("FLEET_PROD_PROBE_HOST", "")
    if not host or not Path(key).exists():
        return f"@@meta\nerror\tno probe access: FLEET_PROD_PROBE_HOST={host or '(unset)'} key={key} exists={Path(key).exists()}\n"
    kh = LOG_DIR / ".prod_probe_known_hosts"
    cmd = ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
           "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={kh}", host, "probe"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0 and "@@" not in r.stdout:
        return f"@@meta\nerror\tssh rc={r.returncode}: {(r.stderr or '').strip()[:200]}\n"
    return r.stdout


def repo_registry(repo_dir: str | None) -> set[str] | None:
    """Job names in the product repo's scripts/box/crontab -- the registry the box should match.
    Commented-out lines are not registered jobs."""
    if not repo_dir:
        return None
    path = Path(repo_dir) / "scripts" / "box" / "crontab"
    if not path.exists():
        return None
    import re
    names = set()
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.search(r"cronjob\.sh ([A-Za-z0-9_.-]+)", line)
        if m:
            names.add(m.group(1))
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="src", help="parse a saved probe capture instead of probing")
    ap.add_argument("--file", action="store_true", help="file one deduped issue per failing check")
    ap.add_argument("--push", action="store_true", help="START/RESOLVE breaches to Reif HQ")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo-dir", default=os.environ.get("FLEET_REPO", "/repo"))
    a = ap.parse_args(argv)

    raw = Path(a.src).read_text() if a.src else fetch()
    (LOG_DIR / "prod_runtime.last.txt").write_text(raw) if LOG_DIR.is_dir() else None
    p = parse(raw)
    findings = evaluate(p, registry=repo_registry(a.repo_dir))
    print(json.dumps(findings, indent=1) if a.json else summary(findings, p))
    if a.file and findings:
        if a.repo_dir and Path(a.repo_dir).is_dir():
            os.chdir(a.repo_dir)  # gh resolves the board repo from the checkout
        for r in file_findings(findings):
            print(f"  filed {r['check']}: {r.get('action')} {r.get('number') or r.get('url') or r.get('error', '')}")
    if a.push:
        host = _kv(p.get("meta", [])).get("host", "prod")
        sent = push_transitions(findings, host, LOG_DIR / "prod_runtime.state.json",
                                LOG_DIR / "prod-alerts.jsonl")
        print(f"  hq push: {len(sent)} transition(s)" + "".join(f"\n    {r['transition'].upper()} {r['signature']}" for r in sent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
