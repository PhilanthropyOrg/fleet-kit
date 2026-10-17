#!/usr/bin/env python3
"""audit_datain -- the data-in lane's checks that need more than one SQL line. (fk8, 2026-10-17)

The lane's first pass (10-17 04:39) ran D1-D4 in part and could not run D5 or D6. Each check
here reads only what the lane can reach:
  d2 completeness: the newest IRS index rows (filing_index) with no row in filings. The index
     has tax_period (YYYYMM), not tax_year: the tax year is the year the period began, so
     202312 -> 2023 and 202306 -> 2022 (the IRS rule; prod's filings rows agree).
  d4 privacy: each sampled org's public JSON (/990/api/org/<ein>/facts) and report page, through
     audit_990_truth.leaks_in (one text node or JSON value at a time, fk7).
  d5 crawler inflation: dash_events is not on the query allowlist, so this reads the flag the
     superadmin Jobs card shows: jobs.real[].crawler_hit in /990/api/signals/kpi (X-PM-Token).
  d6 sources: the newest Search Console point (hub.*gsc* in dash_metric_points) is over 2 days
     old. The IRS index has no date column, so its silence is not checked here.

Usage: audit_datain.py [d2 d4 d5 d6] [--sample sample.jsonl]   (sample = audit_sample.py output)
Prints JSON lines {"check", "finding"} or {"check", "ok"}. A check that cannot run prints
{"check", "error"} plus a stderr line, and the run exits 2: an unrun check is never a clean pass.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_990_truth import _get, leaks_in, privacy_leaks  # noqa: E402
from audit_sample import SITE, query  # noqa: E402

D2_WINDOW = 2000
D2_SQL = ("SELECT i.ein, i.tax_period FROM (SELECT ein, tax_period FROM filing_index "
          f"ORDER BY object_id DESC LIMIT {D2_WINDOW}) i WHERE NOT EXISTS (SELECT 1 FROM filings f "
          "WHERE f.ein = i.ein AND f.tax_year = CAST(substr(i.tax_period, 1, 4) AS INT) - "
          "CASE WHEN substr(i.tax_period, 5, 2) = '12' THEN 0 ELSE 1 END) LIMIT 200")
D6_SQL = "SELECT MAX(ts) AS m FROM dash_metric_points WHERE metric_key LIKE 'hub.%gsc%'"
STALE_DAYS = 2


def d2(q=query) -> list[str]:
    rows = q(D2_SQL)
    if not rows:
        return []
    eg = ", ".join(f"{r['ein']} {r['tax_period']}" for r in rows[:5])
    n = f"{len(rows)}+" if len(rows) >= 200 else str(len(rows))
    return [f"{n} of the newest {D2_WINDOW} IRS index rows have no filings row (e.g. {eg})"]


def _strings(doc) -> list[str]:
    if isinstance(doc, str):
        return [doc]
    vals = doc.values() if isinstance(doc, dict) else doc if isinstance(doc, list) else []
    return [s for v in vals for s in _strings(v)]


def d4(eins: list[str], get=_get) -> list[dict]:
    if not eins:
        raise RuntimeError("no sampled orgs: pass --sample with audit_sample.py output")
    out = []
    for ein in eins:
        for url, read in ((f"{SITE}/990/api/org/{ein}/facts", lambda b: leaks_in(
                _strings(json.loads(b)))), (f"{SITE}/990/report/{ein}", lambda b: privacy_leaks(
                b.decode("utf-8", "replace")))):
            status, _, body = get(url)
            if status != 200:
                out.append({"check": "d4", "error": f"{url} answered HTTP {status}"})
                continue
            out += [{"check": "d4", "finding": f"{url}: {leak}"} for leak in read(body)]
    return out


def kpi() -> dict:
    req = urllib.request.Request(SITE + "/990/api/signals/kpi", headers={
        "User-Agent": "Mozilla/5.0 (fleet auditor)",
        "X-PM-Token": os.environ.get("FLEET_NUMBER_TOKEN", ""),
        "x-atlas-test": os.environ.get("ATLAS_TEST_BYPASS", "")})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def d5(read=kpi) -> list[str]:
    doc = read()
    days = (doc.get("jobs") or {}).get("real")
    if days is None:
        raise RuntimeError("kpi jobs.real is empty: " + str((doc.get("errors") or {}).get(
            "jobs.real") or (doc.get("errors") or {}).get("jobs.funnel") or "no jobs section"))
    return [f"{d['d']}: {d['uncounted']} Apply hits came from no-browser visitors (10x or more the "
            "real ones); a count that includes them is inflated" for d in days if d["crawler_hit"]]


def d6(q=query, now: dt.datetime | None = None) -> list[str]:
    rows = q(D6_SQL)
    last = rows[0].get("m") if rows else None
    if not last:
        raise RuntimeError("no hub.*gsc* points in dash_metric_points at all")
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    age = now - dt.datetime.fromisoformat(str(last)[:19])
    if age.days >= STALE_DAYS:
        return [f"Search Console: the newest hub GSC point is {str(last)[:10]}, "
                f"{age.days} days old"]
    return []


def run(names: list[str], eins: list[str], checks: dict | None = None) -> list[dict]:
    checks = checks or {"d2": d2, "d4": lambda: d4(eins), "d5": d5, "d6": d6}
    out = []
    for name in names:
        try:
            got = checks[name]()
            rows = [g if isinstance(g, dict) else {"check": name, "finding": g} for g in got]
            out += rows or [{"check": name, "ok": True}]
        except Exception as e:  # noqa: BLE001 -- a check that cannot run is reported, not hidden
            out.append({"check": name, "error": f"{type(e).__name__}: {e}"[:300]})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("checks", nargs="*", default=["d2", "d4", "d5", "d6"])
    ap.add_argument("--sample", help="audit_sample.py --lane orgs output (JSON lines)")
    a = ap.parse_args(argv)
    eins = []
    if a.sample:
        eins = [r["ein"] for r in map(json.loads, open(a.sample)) if r.get("ein")]
    rows = run(a.checks, list(dict.fromkeys(eins)))
    for row in rows:
        print(json.dumps(row))
        if "error" in row:
            print(f"audit_datain: CHECK {row['check']} DID NOT RUN: {row['error']}",
                  file=sys.stderr)
    return 2 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
