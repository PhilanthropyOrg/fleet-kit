#!/usr/bin/env python3
"""reif_eyes.py -- looks at the fleet the way Reif does, hourly, and files what he would.

Reif, 2026-09-16, after pointing out four things in one sitting (a churning member, a jargon
report, an ask nobody worked for 13 hours, tiles saying "history starts today" while the
sources had history): "the worst part is I shouldnt be asking for it, something should be
thinking about these things itself."

This is that something. Deterministic, no model, no spend: it reads runs.jsonl, fleet.db's
asks and the console's own /api/metrics, applies one check per thing he has had to point out,
and files ONE fleet:backlog + fleet:priority-high issue per finding in the product repo so a
builder picks it up. Idempotent: a finding key that already has an open issue is skipped.

THE LEDGER -- every check is a complaint he made, dated. Add a check when he makes a new one;
never delete one without his word.
  churn        2026-09-16 "some churning" -- a member starting many times an hour and
                          finishing almost nothing (the-fixer: 283 runs / 9 ok in 6h).
  ask-stale    2026-09-16 "make darn sure that another agent picks it up" -- an ask open
                          more than 24h with no board issue in its name.
  tile-dark    2026-09-16 "we have history for all of these" -- a console tile with no
                          value, or a daily-history tile whose sparkline is still empty a
                          day after its first point.
  no-plain     2026-09-16 "this needs to be in plain english" -- finished runs whose record
                          carries no plain-English words while FLEET_RUN_PLAIN=1.

Usage: reif_eyes.py [--dry-run] [--hours 6]
Env: FLEET_LOG_DIR, FLEET_REPO_URL, FLEET_VIEW_PORT (default 8420), FLEET_RUN_PLAIN.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
STATE = LOG_DIR / ".reif_eyes.state"
LABELS = "fleet:backlog,fleet:priority-high,lane:fleet"


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] {msg}")


# ---------------------------------------------------------------- checks (pure)

def check_churn(runs: list[dict], now: float, hours: float = 6.0) -> list[dict]:
    """A member that started >= 12 times in the window and finished ok on fewer than a
    quarter of them, or was dispatch_skipped >= 20 times, is churning."""
    since = now - hours * 3600
    per: dict[str, dict[str, int]] = {}
    for r in runs:
        if (r.get("ts") or 0) < since or not r.get("member"):
            continue
        c = per.setdefault(r["member"], {})
        c[r.get("status") or "?"] = c.get(r.get("status") or "?", 0) + 1
    out = []
    for m, c in sorted(per.items()):
        started, ok, skipped = c.get("started", 0), c.get("ok", 0), c.get("dispatch_skipped", 0)
        if (started >= 12 and ok * 4 < started) or skipped >= 20:
            out.append({"key": f"churn:{m}",
                        "title": f"{m} is churning: {started} starts, {ok} ok, {skipped} dispatch-skipped in {int(hours)}h",
                        "body": (f"In the last {int(hours)}h `{m}` started {started} times and finished ok {ok} times "
                                 f"({skipped} dispatch_skipped, {c.get('quiet', 0)} quiet, {c.get('budget_declined', 0)} budget_declined, "
                                 f"{c.get('killed', 0)} killed). Every start is a paid pass. Find what launches it "
                                 f"beyond its schedule and cap it; a run that will do nothing should not start.\n\n"
                                 f"Counts: `{json.dumps(c, sort_keys=True)}`")})
    return out


def check_ask_stale(asks: list[dict], open_issue_titles: list[str], now: float, hours: float = 24.0) -> list[dict]:
    out = []
    for a in asks:
        age_h = (now - (a.get("filed_at") or now)) / 3600
        if a.get("status") != "open" or age_h < hours:
            continue
        if any(f"ask #{a['id']} " in t or t.endswith(f"ask #{a['id']}") for t in open_issue_titles):
            continue
        why = (a.get("why") or "").strip().splitlines()[0][:90] if a.get("why") else "(no why)"
        out.append({"key": f"ask-stale:{a['id']}",
                    "title": f"ask #{a['id']} ({a.get('class') or 'unclassed'}): {why}",
                    "body": (f"Fleet ask #{a['id']} from `{a.get('member')}` has been open {int(age_h)}h with no board "
                             f"issue in its name, so no agent has tried it.\n\n**Why a human was asked:** "
                             f"{(a.get('why') or '').strip()}\n\n"
                             + (f"**What it unblocks:** {a['unblocks'].strip()}\n\n" if a.get("unblocks") else "")
                             + (f"**Proposed:** {a['proposed'].strip()}\n\n" if a.get("proposed") else "")
                             + "Try it first. If it truly needs a person (a secret, money, an account), say exactly "
                               "what and label `fleet:needs-human-op`; otherwise fix it and close this.")})
    return out


def check_tile_dark(metrics: dict, registry: list[dict], now: float) -> list[dict]:
    out = []
    reg = {m["id"]: m for m in registry}
    if isinstance(metrics, list):  # /api/metrics serves a list in display order, keyed by id
        metrics = {m.get("id"): m for m in metrics if isinstance(m, dict) and m.get("id")}
    for mid, m in (metrics or {}).items():
        spec = reg.get(mid, {})
        series = m.get("series") or []
        if m.get("value") is None and spec.get("history") != "native":
            out.append({"key": f"tile-dark:{mid}",
                        "title": f"console tile '{spec.get('label', mid)}' shows no value ({m.get('sub') or 'no reason given'})",
                        "body": f"`/api/metrics` returns `value: null` for `{mid}` (sub: {m.get('sub')!r}). A tile Reif looks at every day is dark. Source: {spec.get('source')}."})
        elif spec.get("history") == "daily" and len(series) < 2 and series:
            first_day = series[0].get("day", "")
            try:
                first_ts = time.mktime(time.strptime(first_day, "%Y-%m-%d"))
            except ValueError:
                continue
            if now - first_ts > 36 * 3600:
                out.append({"key": f"tile-dark:{mid}",
                            "title": f"console tile '{spec.get('label', mid)}' still has no history",
                            "body": f"`{mid}` has had a daily history row since {first_day} but the sparkline still has {len(series)} point(s). Source `{spec.get('source')}` has its own history (created/closed dates); backfill it instead of waiting."})
    return out


def gated_accounts(state_text: str, accounts: list[str], now: float) -> list[tuple[str, float]]:
    """(account, reset_epoch) for every configured account the pool state file still gates."""
    until: dict[str, float] = {}
    for line in state_text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                until[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return [(a, until[a]) for a in accounts if a in until and until[a] > now]


def check_pool_exhausted(state_text: str, accounts: list[str], now: float) -> list[dict]:
    """Every account in FLEET_ACCOUNTS is gated: the fleet is dark and every other symptom
    (budget_declined runs, no plain words, quiet members) is this one fact. One finding, not six."""
    gated = gated_accounts(state_text, accounts, now)
    if not accounts or len(gated) < len(accounts):
        return []
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(t))
    soonest = min(t for _, t in gated)
    return [{"key": "pool-exhausted",
             "title": f"the fleet is dark: every account is at its weekly limit until {fmt(soonest)}",
             "body": ("Every account in the pool is gated by `account-pool-exhausted.state`: "
                      + "; ".join(f"`{a}` until {fmt(t)}" for a, t in gated)
                      + ". Until the first reset no member can spend a call, so every run reads `budget_declined` "
                        "and no report gets plain words. Either add an account to FLEET_ACCOUNTS or wait; "
                        "nothing on the board moves before then.")}]


def check_no_plain(runs: list[dict], now: float, hours: float = 24.0, pool_dark: bool = False) -> list[dict]:
    if os.environ.get("FLEET_RUN_PLAIN") != "1" or pool_dark:
        return []
    since = now - hours * 3600
    per: dict[str, list[int]] = {}
    for r in runs:
        if (r.get("ts") or 0) < since or r.get("status") != "ok" or not (r.get("report") or r.get("outcome")):
            continue
        c = per.setdefault(r.get("member") or "?", [0, 0])
        c[0] += 1
        if not r.get("plain"):
            c[1] += 1
    out = []
    for m, (total, missing) in sorted(per.items()):
        if missing >= 3 and missing * 2 >= total:
            out.append({"key": f"no-plain:{m}",
                        "title": f"{m}'s reports have no plain-English words ({missing} of {total} finished runs in {int(hours)}h)",
                        "body": f"FLEET_RUN_PLAIN=1 is set, yet {missing} of `{m}`'s last {total} finished runs carry no `plain` field. The console drawer opens on jargon. Check run_report.py's plain_words_for path for this member (budget cap, account pool, timeout)."})
    return out


# ---------------------------------------------------------------- inputs

def load_runs(path: pathlib.Path | None = None) -> list[dict]:
    p = path or (LOG_DIR / "runs.jsonl")
    rows = []
    try:
        for line in p.read_text(errors="ignore").splitlines()[-5000:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return rows


def load_asks() -> list[dict]:
    try:
        sys.path.insert(0, str(HERE))
        import ask, fleet_db  # noqa: E402
        conn = fleet_db.connect()
        try:
            return ask.list_asks(conn, status="open", limit=200)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log(f"asks unreadable: {exc}")
        return []


def load_metrics() -> tuple[dict, list[dict]]:
    port = os.environ.get("FLEET_VIEW_PORT", "8420")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/metrics", timeout=20) as r:
            metrics = json.load(r).get("metrics") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"metrics unreadable: {exc}")
        metrics = {}
    try:
        registry = json.loads((HERE / "metrics.json").read_text())["metrics"]
    except Exception:  # noqa: BLE001
        registry = []
    return metrics, registry


def repo_slug() -> str:
    url = (os.environ.get("FLEET_REPO_URL") or "").strip()
    slug = url.rsplit("github.com", 1)[-1].lstrip(":/").removesuffix(".git").strip("/")
    return slug if slug.count("/") == 1 and all(slug.split("/")) else ""


def _run(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def open_issue_titles(slug: str, run=_run) -> list[str]:
    r = run(["gh", "issue", "list", "--repo", slug, "--state", "open", "--limit", "300", "--json", "title", "--jq", ".[].title"])
    return [t for t in (r.stdout or "").splitlines() if t.strip()] if r.returncode == 0 else []


# ---------------------------------------------------------------- filing

def file_findings(findings: list[dict], slug: str, state: dict, titles: list[str], run=_run, now: float | None = None) -> list[str]:
    """One issue per new finding key. A key filed in the last 7 days, or whose title is
    already open, is skipped. Returns the URLs filed."""
    now = now or time.time()
    urls = []
    for f in findings:
        prior = state.get(f["key"])
        if prior and now - prior.get("ts", 0) < 7 * 86400:
            continue
        if any(t.strip() == f["title"].strip() for t in titles):
            state[f["key"]] = {"ts": now, "url": "(already open)"}
            continue
        body = f["body"] + f"\n\n<!-- reif-eyes:{f['key']} -->\nFiled by reif_eyes.py, the pass that looks at the console the way Reif does."
        r = run(["gh", "issue", "create", "--repo", slug, "--title", f["title"], "--label", LABELS, "--body", body])
        if r.returncode == 0 and r.stdout.strip():
            url = r.stdout.strip().splitlines()[-1]
            state[f["key"]] = {"ts": now, "url": url}
            urls.append(url)
            log(f"filed {f['key']}: {url}")
        else:
            log(f"could not file {f['key']}: {(r.stderr or r.stdout).strip()[:200]}")
    return urls


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    except OSError as exc:
        log(f"state not saved: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print findings, file nothing")
    ap.add_argument("--hours", type=float, default=6.0)
    a = ap.parse_args(argv)
    now = time.time()
    runs = load_runs()
    slug = repo_slug()
    titles = open_issue_titles(slug) if slug and not a.dry_run else []
    metrics, registry = load_metrics()
    accounts = (os.environ.get("FLEET_ACCOUNTS") or "").split()
    try:
        pool_state = (LOG_DIR / "account-pool-exhausted.state").read_text()
    except OSError:
        pool_state = ""
    dark = check_pool_exhausted(pool_state, accounts, now)
    findings = (dark + check_churn(runs, now, a.hours) + check_ask_stale(load_asks(), titles, now)
                + check_tile_dark(metrics, registry, now) + check_no_plain(runs, now, pool_dark=bool(dark)))
    log(f"{len(findings)} finding(s): " + ", ".join(f["key"] for f in findings))
    if a.dry_run:
        json.dump(findings, sys.stdout, indent=1); print()
        return 0
    if not slug:
        log("FLEET_REPO_URL does not name a GitHub repo; nothing filed")
        return 0
    state = load_state()
    file_findings(findings, slug, state, titles, now=now)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
