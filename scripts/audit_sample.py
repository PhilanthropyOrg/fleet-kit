#!/usr/bin/env python3
"""audit_sample -- the auditor's stratified, rotating sample of 20 orgs (or 20 jobs) per run.

One query per stratum, read-only, through the fleet's existing prod SQL surface
(POST /990/api/query with X-PM-Token=$FLEET_NUMBER_TOKEN and x-atlas-test=$ATLAS_TEST_BYPASS;
the endpoint itself allows one SELECT and a 3s timeout). The OFFSET is the run slot
(2h windows since the epoch), so each run reads a different org inside every stratum and the
whole range is covered over days, not the same 20 forever.

Usage: audit_sample.py --lane orgs|jobs [--slot N] [--print-sql]
Prints JSON lines {"stratum", "ein", "name" | "pid", ...}. A stratum whose query fails prints
{"stratum", "error"} -- an unreadable stratum is a finding, not a skip.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

API = os.environ.get("AUDIT_QUERY_URL", "https://philanthropy.org/990/api/query")

# Viken's set is fixed every run: the holes he reported must stay closed.
VIKEN = ["362193608", "363258696"]  # Shriners, YMCA of the USA; Salvation Army rotates below

_REV = ("SELECT o.ein, o.name FROM organizations o JOIN filings f ON f.ein = o.ein "
        "WHERE {where} GROUP BY o.ein, o.name ORDER BY {order} LIMIT {limit} OFFSET {off}")
ORG_STRATA = {
    "largest": (_REV, "f.tax_year >= 2022", "MAX(f.total_revenue) DESC", 10, 1),
    "hospital": (_REV, "o.ntee_code IN ('E20','E21','E22','E24')", "MAX(f.total_revenue) DESC", 1, 50),
    "church_no_990": ("SELECT o.ein, o.name FROM organizations o WHERE o.ntee_code LIKE 'X2%' "
                      "AND NOT EXISTS (SELECT 1 FROM filings f WHERE f.ein = o.ein) "
                      "ORDER BY o.ein LIMIT {limit} OFFSET {off}", "", "", 1, 5000),
    "foundation_daf": ("SELECT i.ein, o.name FROM filing_index i JOIN organizations o "
                       "ON o.ein = i.ein WHERE i.return_type = '990PF' ORDER BY i.object_id "
                       "DESC LIMIT {limit} OFFSET {off}", "", "", 1, 500),
    "small_under_200k": (_REV, "f.total_revenue BETWEEN 1 AND 199999", "o.ein", 1, 5000),
    "filed_this_week": ("SELECT i.ein, o.name FROM filing_index i JOIN organizations o "
                        "ON o.ein = i.ein ORDER BY i.object_id DESC LIMIT {limit} OFFSET {off}",
                        "", "", 2, 50),
    "salvation_army": ("SELECT ein, name FROM organizations WHERE name LIKE 'SALVATION ARMY%' "
                       "ORDER BY ein LIMIT {limit} OFFSET {off}", "", "", 1, 40),
    "viken": ("SELECT ein, name FROM organizations WHERE ein IN ('" + "','".join(VIKEN) + "') "
              "ORDER BY ein LIMIT {limit} OFFSET {off}", "", "", 2, 1),
}
_JOB = ("SELECT pid, ein, title, ats, source_url, apply_url, closed_at FROM job_postings "
        "WHERE {where} ORDER BY {order} LIMIT {limit} OFFSET {off}")
JOB_STRATA = {
    "newest": (_JOB, "closed_at IS NULL", "first_seen DESC", 5, 20),
    "leadership": (_JOB, "closed_at IS NULL AND title_family IN ('executive','leadership')",
                   "first_seen DESC", 3, 200),
    "workday": (_JOB, "closed_at IS NULL AND ats = 'workday'", "first_seen DESC", 3, 500),
    "greenhouse": (_JOB, "closed_at IS NULL AND ats = 'greenhouse'", "first_seen DESC", 3, 500),
    "lever": (_JOB, "closed_at IS NULL AND ats = 'lever'", "first_seen DESC", 3, 500),
    "closed": (_JOB, "closed_at IS NOT NULL", "closed_at DESC", 3, 200),
}


def sql_for(strata: dict, slot: int) -> dict[str, str]:
    out = {}
    for name, (tmpl, where, order, limit, span) in strata.items():
        off = (slot * limit) % span if span > 1 else 0
        out[name] = tmpl.format(where=where, order=order, limit=limit, off=off)
    return out


def query(sql: str) -> list[dict]:
    req = urllib.request.Request(
        API, data=json.dumps({"sql": sql, "limit": 50}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (fleet auditor)",
                 "X-PM-Token": os.environ.get("FLEET_NUMBER_TOKEN", ""),
                 "x-atlas-test": os.environ.get("ATLAS_TEST_BYPASS", "")})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    return data.get("rows", data if isinstance(data, list) else [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", choices=("orgs", "jobs"), default="orgs")
    ap.add_argument("--slot", type=int, default=int(time.time() // 7200))
    ap.add_argument("--print-sql", action="store_true")
    a = ap.parse_args(argv)
    for stratum, sql in sql_for(ORG_STRATA if a.lane == "orgs" else JOB_STRATA, a.slot).items():
        if a.print_sql:
            print(json.dumps({"stratum": stratum, "sql": sql}))
            continue
        try:
            for row in query(sql):
                print(json.dumps({"stratum": stratum, **row}))
        except Exception as e:  # noqa: BLE001 -- an unreadable stratum is reported, not hidden
            print(json.dumps({"stratum": stratum, "error": f"{type(e).__name__}: {e}"[:200]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
