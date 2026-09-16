#!/usr/bin/env python3
"""signals_pull.py -- the fleet's daily read of the product's datafeed (fk#1042).

Reif, 2026-09-16: "how do we think about the datafeed? who is consuming the data with which to
build insights from?" -- nobody was. This pulls, deterministically and with no model, the four
token-gated reads the product already serves (the number, the claim funnel + OKR readings,
PostHog top events, GA4 landing pages), stores the day's snapshot under
$FLEET_LOG_DIR/signals/<date>.json, and renders one compact block -- today, and what changed
since the previous snapshot -- that the `signals` member reads before it reasons.

  signals_pull.py --fetch    -> write today's snapshot (each read best-effort; a failed read
                                is recorded as an error, never a crash)
  signals_pull.py --render   -> print the block for the prompt (fetches first unless --no-fetch)

Env: FLEET_NUMBER_URL (…/api/number; the base is derived from it), FLEET_NUMBER_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request

LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
SIG_DIR = LOG_DIR / "signals"
READS = {
    "number": "/api/number",
    "funnel": "/api/signals/funnel",
    "posthog": "/api/signals/posthog",
    "ga4": "/api/signals/ga4",
}


def base_url() -> str:
    url = (os.environ.get("FLEET_NUMBER_URL") or "").strip()
    return url[: url.rfind("/api/")] if "/api/" in url else ""


def fetch_one(path: str, timeout: float = 40.0) -> dict:
    token = os.environ.get("FLEET_NUMBER_TOKEN", "").strip()
    req = urllib.request.Request(base_url() + path, headers={"X-PM-Token": token, "User-Agent": "fleet-kit/signals_pull"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_all(fetch=fetch_one) -> dict:
    snap: dict = {"fetched_at": time.time(), "reads": {}, "errors": {}}
    if not base_url():
        snap["errors"]["base"] = "FLEET_NUMBER_URL unset or has no /api/ segment"
        return snap
    for key, path in READS.items():
        try:
            snap["reads"][key] = fetch(path)
        except Exception as exc:  # noqa: BLE001
            snap["errors"][key] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return snap


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def save(snap: dict) -> pathlib.Path:
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    p = SIG_DIR / f"{day_of(snap['fetched_at'])}.json"
    p.write_text(json.dumps(snap, indent=1))
    return p


def previous(today: str) -> dict | None:
    try:
        days = sorted(q.stem for q in SIG_DIR.glob("*.json") if q.stem < today)
    except OSError:
        return None
    if not days:
        return None
    try:
        return json.loads((SIG_DIR / f"{days[-1]}.json").read_text())
    except Exception:  # noqa: BLE001
        return None


def _g(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _delta(now, before):
    if isinstance(now, (int, float)) and isinstance(before, (int, float)):
        d = now - before
        return f" ({'+' if d >= 0 else ''}{round(d, 1)} since last snapshot)"
    return ""


def render(snap: dict, prev: dict | None) -> str:
    r, pr = snap.get("reads", {}), (prev or {}).get("reads", {})
    out = [f"# SIGNALS -- the product's datafeed, read {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(snap.get('fetched_at', 0)))}"
           + (f" (previous snapshot {day_of(prev['fetched_at'])})" if prev else " (no previous snapshot: first day)")]
    n = _g(r, "number")
    if n:
        num = n.get("number") or {}
        out.append(f"- Objective: {num.get('value')} {num.get('unit', '')} ({'+' if (num.get('delta_7d') or 0) >= 0 else ''}{num.get('delta_7d')} in 7d)"
                   + _delta(num.get("value"), _g(pr, "number", "number", "value")))
        k1 = n.get("kr1") or {}
        if k1.get("value") is not None:
            out.append(f"- Claims started, last 7d: {k1.get('value')} (completion {k1.get('completion_rate_pct')}%, pending {k1.get('pending')}, median pending age {k1.get('median_pending_age_days')}d)"
                       + _delta(k1.get("value"), _g(pr, "number", "kr1", "value")))
        ent = n.get("entities") or {}
        if ent.get("value") is not None:
            out.append(f"- Entities with a real interaction: {ent.get('value')} ({'+' if (ent.get('delta_7d') or 0) >= 0 else ''}{ent.get('delta_7d')} in 7d)")
    f = _g(r, "funnel")
    if f:
        okr = f.get("okr") or {}
        for key in ("kr2", "kr3"):
            kr = okr.get(key) or {}
            if kr.get("value") is not None:
                out.append(f"- {key.upper()} {kr.get('name')}: {kr.get('value')} {kr.get('unit', '')}" + _delta(kr.get("value"), _g(pr, "funnel", "okr", key, "value")))
        fu = f.get("funnel") or {}
        stages = fu.get("stages") or []
        if stages:
            out.append("- Funnel (distinct orgs, last %s days): " % fu.get("days") + " -> ".join(f"{s.get('label') or s.get('key')} {s.get('orgs')}" for s in stages))
        ws = fu.get("worst_step") or {}
        if ws.get("to_label"):
            out.append(f"- Worst step: {ws.get('from_label')} -> {ws.get('to_label')} loses {ws.get('lost')} orgs ({ws.get('drop_pct')}%)")
        if f.get("errors"):
            out.append(f"- funnel feed errors: {f['errors']}")
    ph = _g(r, "posthog")
    if ph and ph.get("top_events"):
        top = ph["top_events"][:6]
        out.append("- PostHog top events (24h): " + ", ".join(f"{e.get('event', e.get('name', '?'))} {e.get('count', e.get('total', ''))}" for e in top if isinstance(e, dict)))
    ga = _g(r, "ga4")
    if ga and ga.get("sessions_today") is not None:
        out.append(f"- GA4 sessions today: {ga.get('sessions_today')}" + _delta(ga.get("sessions_today"), _g(pr, "ga4", "sessions_today")))
        pages = ga.get("top_landing_pages") or []
        if pages:
            out.append("- Top landing pages: " + "; ".join(f"{p.get('page', p.get('path', '?'))} {p.get('sessions', '')}" for p in pages[:5] if isinstance(p, dict)))
    if snap.get("errors"):
        out.append("- READS THAT FAILED (a broken instrument -- name it in Broken:): " + json.dumps(snap["errors"]))
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    today = day_of(time.time())
    snap = None
    if a.fetch or (a.render and not a.no_fetch):
        snap = fetch_all()
        p = save(snap)
        if a.fetch and not a.render:
            print(f"wrote {p} ({len(snap['reads'])} reads, {len(snap['errors'])} errors)")
    if a.render:
        if snap is None:
            try:
                snap = json.loads((SIG_DIR / f"{today}.json").read_text())
            except Exception:  # noqa: BLE001
                print("signals_pull: no snapshot for today and --no-fetch given", file=sys.stderr)
                return 1
        print(render(snap, previous(today)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
