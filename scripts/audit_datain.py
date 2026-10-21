#!/usr/bin/env python3
"""audit_datain -- the data-in lane's checks that need more than one SQL line. (fk8, 2026-10-17)

The lane's first pass (10-17 04:39) ran D1-D4 in part and could not run D5 or D6. Each check
here reads only what the lane can reach:
  d1 freshness: every dash_metric_points key (latest:: lists aside) whose newest point is over
     D1_STALE_DAYS behind the newest point of any key. A writer marked "off" in crons.md is not
     an excuse: the dashboard still shows its number as current (#13429, 35 keys frozen 27 days
     while D1 was a by-hand query). The query caps at 50 rows, so 50 reads as "50+".
  d2 completeness: the newest IRS index rows (filing_index) with no row in filings. The index
     has tax_period (YYYYMM), not tax_year: the tax year is the year the period began, so
     202312 -> 2023 and 202306 -> 2022 (the IRS rule; prod's filings rows agree).
  d4 privacy: each sampled org's public JSON (/990/api/org/<ein>/facts) address fields and its
     report page's address lines, through audit_990_truth.leaks_in (one text node or JSON value
     at a time, fk7). The org's name is never read as a street (fk11: "1638-1640 Topping Ave").
  d5 crawler inflation: dash_events is not on the query allowlist, so this reads the flag the
     superadmin Jobs card shows: jobs.real[].crawler_hit in /990/api/signals/kpi (X-PM-Token).
  d6 sources: the newest Search Console point (hub.*gsc* in dash_metric_points) is over 2 days
     old. The IRS index has no date column, so its silence is not checked here.
  d7 rollup lag: the next hop after d2. List pages (/990/nonprofits/largest, rankings, cards)
     read organizations.fin_*, the report page reads filings, so a rollup that was not re-run
     shows last year's revenue on the list while the report is right (#13317: Fidelity
     Charitable $19.0B listed, $30.6B filed; 13 of the top 30). Checks the top D7_TOP by revenue.

Usage: audit_datain.py [d1 d2 d4 d5 d6 d7] [--sample sample.jsonl]   (sample = audit_sample.py output)
Prints JSON lines {"check", "finding"} or {"check", "ok"}. A check that cannot run prints
{"check", "error"} plus a stderr line, and the run exits 2: an unrun check is never a clean pass.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_990_truth import _get, leaks_in, privacy_leaks  # noqa: E402
from audit_cleanup import state_path  # noqa: E402
from audit_sample import SITE, query  # noqa: E402

D2_WINDOW = 2000
D2_SQL = ("SELECT i.ein, i.tax_period FROM (SELECT ein, tax_period FROM filing_index "
          f"ORDER BY object_id DESC LIMIT {D2_WINDOW}) i WHERE NOT EXISTS (SELECT 1 FROM filings f "
          "WHERE f.ein = i.ein AND f.tax_year = CAST(substr(i.tax_period, 1, 4) AS INT) - "
          "CASE WHEN substr(i.tax_period, 5, 2) = '12' THEN 0 ELSE 1 END) LIMIT 200")
D1_STALE_DAYS = 3
D1_SQL = ("SELECT metric_key, MAX(ts) AS m FROM dash_metric_points WHERE metric_key NOT LIKE "
          "'latest::%' GROUP BY metric_key HAVING MAX(ts)::timestamp < (SELECT MAX(ts)::timestamp "
          f"FROM dash_metric_points) - INTERVAL '{D1_STALE_DAYS} days' ORDER BY 2 LIMIT 50")
D6_SQL = "SELECT MAX(ts) AS m FROM dash_metric_points WHERE metric_key LIKE 'hub.%gsc%'"
STALE_DAYS = 2
D7_TOP = 200
D7_SQL = ("SELECT ein, fin_year, y FROM (SELECT o.ein, o.fin_year, (SELECT MAX(f.tax_year) FROM "
          "filings f WHERE f.ein = o.ein AND f.total_revenue IS NOT NULL) AS y FROM (SELECT ein, "
          "fin_year FROM organizations WHERE fin_revenue IS NOT NULL ORDER BY fin_revenue DESC "
          f"LIMIT {D7_TOP}) o) t WHERE y > fin_year OR (fin_year IS NULL AND y IS NOT NULL)")


def d1(q=query) -> list[str]:
    rows = q(D1_SQL)
    if not rows:
        return []
    n = "50+" if len(rows) >= 50 else str(len(rows))
    eg = ", ".join(f"{r['metric_key']} {str(r['m'])[:10]}" for r in rows[:5])
    return [f"{n} dashboard keys have no point in the last {D1_STALE_DAYS} days of the newest "
            f"one; the oldest froze {str(rows[0]['m'])[:10]} (e.g. {eg})"]


def d2(q=query) -> list[str]:
    rows = q(D2_SQL)
    if not rows:
        return []
    eg = ", ".join(f"{r['ein']} {r['tax_period']}" for r in rows[:5])
    n = f"{len(rows)}+" if len(rows) >= 200 else str(len(rows))
    return [f"{n} of the newest {D2_WINDOW} IRS index rows have no filings row (e.g. {eg})"]


_ADDRESS_KEY = re.compile(r"addr|street|location|contact|mail|phone", re.I)


def _address_strings(doc, inside: bool = False) -> list[str]:
    """String values under an address-bearing key (identity.address, officers[].address, a
    contact block), never the name: a legal name can read like a street (EIN 133847940)."""
    if isinstance(doc, str):
        return [doc] if inside else []
    if isinstance(doc, dict):
        return [s for k, v in doc.items()
                for s in _address_strings(v, inside or bool(_ADDRESS_KEY.search(str(k))))]
    return [s for v in doc for s in _address_strings(v, inside)] if isinstance(doc, list) else []


def _facts_leaks(body: bytes) -> list[str]:
    doc = json.loads(body)
    return leaks_in(_address_strings(doc), str(doc.get("name") or ""))


def d4(eins: list[str], get=_get, seen_path: Path | None = None) -> list[dict]:
    """One finding per leaking org, naming every leak once. seen_path keeps how many passes each
    EIN has leaked, so a repeat reads "seen N passes" instead of looking new."""
    if not eins:
        raise RuntimeError("no sampled orgs: pass --sample with audit_sample.py output")
    seen_path = seen_path or state_path().with_name("datain-d4-seen.json")
    try:
        seen = json.loads(seen_path.read_text())
    except (OSError, ValueError):
        seen = {}
    out = []
    for ein in eins:
        leaks = []
        for url, read in ((f"{SITE}/990/api/org/{ein}/facts", _facts_leaks),
                          (f"{SITE}/990/report/{ein}", lambda b: privacy_leaks(
                              b.decode("utf-8", "replace"), address_only=True))):
            status, _, body = get(url)
            if status != 200:
                out.append({"check": "d4", "error": f"{url} answered HTTP {status}"})
                continue
            leaks += read(body)
        if leaks:
            seen[ein] = seen.get(ein, 0) + 1
            out.append({"check": "d4", "finding": f"{SITE}/990/report/{ein}: "
                        + "; ".join(dict.fromkeys(leaks)) + f" (seen {seen[ein]} passes)"})
    try:
        seen_path.write_text(json.dumps(seen))
    except OSError as e:
        print(f"audit_datain: d4 seen-set not saved ({e})", file=sys.stderr)
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


def d7(q=query) -> list[str]:
    rows = q(D7_SQL)
    if not rows:
        return []
    eg = ", ".join(f"{r['ein']} shows {r['fin_year']}, filed {r['y']}" for r in rows[:5])
    n = f"{len(rows)}+" if len(rows) >= 50 else str(len(rows))  # /990/api/query caps at 50 rows
    return [f"{n} of the {D7_TOP} largest orgs show an older year's figures on list pages than "
            f"their newest filing (organizations.fin_year < filings.tax_year; e.g. {eg})"]


def run(names: list[str], eins: list[str], checks: dict | None = None) -> list[dict]:
    checks = checks or {"d1": d1, "d2": d2, "d4": lambda: d4(eins), "d5": d5, "d6": d6, "d7": d7}
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
    ap.add_argument("checks", nargs="*", default=["d1", "d2", "d4", "d5", "d6", "d7"])
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
