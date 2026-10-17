#!/usr/bin/env python3
"""product_metrics.py -- the numbers each company is for, so a bet can name one.

Desk, 2026-10-17: of dumbledore's 25 bets since 10-09, 3 touched the product and none the
vision scoreboard; the rest were avg_turns, quiet_rate, runs_per_day, ci_minutes. Reif:
"Ok but did the experience improve for the user". These are the user-facing numbers, read
hourly by north.py into $FLEET_LOG_DIR/product.jsonl ({ts, stages: {name: value}}, the same
shape as ladder.jsonl) and scored by fleet_metrics.py `product:<name>`.

SOURCES (our own tables, never PostHog -- shut down 10-07):
  po.*   philanthropy.org /990/api/signals/kpi (X-PM-Token = FLEET_NUMBER_TOKEN)
  dyw.*  $FLEET_G_SHARE/dyw-metrics.json       G's host cron, from recruit.db
  gym.*  $FLEET_G_SHARE/gymfleet-metrics.json  G's host cron
A drop is {"ts": epoch, "<name>": number, ...}; one older than DROP_MAX_AGE_S is no reading.
A number the source does not carry is left out (unavailable), never 0.

  record   fetch, write one snapshot line, print it
  show     print the latest snapshot
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DROP_MAX_AGE_S = 36 * 3600
DROPS = {"dyw": ("dyw-metrics.json", ("applicants_per_day", "hires_per_week", "meta_spend_per_applicant")),
         "gym": ("gymfleet-metrics.json", ("active_gyms", "machines_tracked", "members"))}
LOWER = {"dyw.meta_spend_per_applicant"}  # every other product number: higher is better


def snapshot_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "product.jsonl"


def share_dir() -> Path:
    return Path(os.environ.get("FLEET_G_SHARE", "/fleet-kit/shares/g"))


def _get(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d if isinstance(d, (int, float)) and not isinstance(d, bool) else None


def from_kpi(kpi: dict, now: float) -> dict:
    """philanthropy.org's scoreboard out of one /api/signals/kpi document."""
    out = {
        "po.paid_verified_orgs": _get(kpi, "claims", "verified_orgs"),
        "po.claims_started_24h": _get(kpi, "claims", "funnel", "totals_24h", "claim_page"),
        "po.claims_finished_24h": _get(kpi, "claims", "funnel", "totals_24h", "submitted"),
    }
    recent = ((kpi.get("money") or {}).get("recent")) if isinstance(kpi, dict) else None
    if isinstance(recent, list):
        week = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * 86400))
        out["po.scout_plus_sales_7d"] = sum(
            1 for r in recent if isinstance(r, dict) and r.get("kind") == "checkout"
            and r.get("status") == "complete" and "scout+" in str(r.get("tier", "")).lower()
            and str(r.get("t", "")) >= week)
    # philanthropy#12900: real browsers only, per UTC day. Until it lands: applications here
    # and Search Console clicks to job pages; the 2nd-page share has no source before it.
    real = _get_list(kpi, "jobs", "funnel", "real")[-8:-1]  # the last 7 full days
    if real:
        google = sum(int(x.get("google") or 0) for x in real)
        out["po.job_applies_7d"] = sum(int(x.get("apply_out") or 0) + int(x.get("applied_here") or 0)
                                       for x in real if not x.get("crawler_hit"))
        out["po.jobs_from_google_7d"] = google
        out["po.jobs_second_page_pct"] = 100.0 * sum(int(x.get("second_page") or 0) for x in real) / google \
            if google else None
    else:
        out["po.job_applies_7d"] = _get(kpi, "jobs", "applications", "last_7d")
        out["po.jobs_from_google_7d"] = _get(kpi, "jobs", "search", "clicks_7d")
    return {k: v for k, v in out.items() if v is not None}


def _get_list(d, *path) -> list:
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def from_drops(now: float, d: Path | None = None) -> dict:
    out = {}
    for prefix, (fname, names) in DROPS.items():
        try:
            doc = json.loads(((d or share_dir()) / fname).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or now - float(doc.get("ts") or 0) > DROP_MAX_AGE_S:
            continue
        out.update({f"{prefix}.{n}": doc[n] for n in names if _get(doc, n) is not None})
    return out


def record(kpi: dict, now: float | None = None, path: Path | None = None) -> dict:
    now = time.time() if now is None else now
    values = {**from_kpi(kpi or {}, now), **from_drops(now)}
    if values:
        p = path or snapshot_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": now, "stages": values}) + "\n")
    return values


def kpi_url() -> str:
    base = (os.environ.get("FLEET_NUMBER_URL") or "").rsplit("/api/", 1)[0]
    return f"{base}/api/signals/kpi" if base else ""


def main(argv: list[str]) -> int:
    if argv[1:2] == ["record"]:
        import north
        kpi, err = north.load_funnel(url=kpi_url() or None)
        if err:
            print(f"product_metrics: kpi unreadable: {err}", file=sys.stderr)
        print(json.dumps(record(kpi)))
        return 0
    if argv[1:2] == ["show"]:
        p = snapshot_path()
        print(p.read_text().splitlines()[-1] if p.exists() and p.read_text().strip() else "no snapshot yet")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv))
