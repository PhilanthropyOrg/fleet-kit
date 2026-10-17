#!/usr/bin/env python3
"""audit_sample -- the auditor's stratified, rotating sample of 20 orgs (or 20 jobs) per run.

Jobs (--lane jobs) read the public pages, not SQL: newest (/jobs/all), highest_paid (the top
median role in /jobs#pay), leadership (/jobs/leadership-openings), one per ATS (the /go hop's
Location host), closed (pids from an earlier pass, kept in $AUDIT_JOBS_STATE, now 410).

Orgs: one query per stratum, read-only, through the fleet's existing prod SQL surface
(POST /990/api/query with X-PM-Token=$FLEET_NUMBER_TOKEN and x-atlas-test=$ATLAS_TEST_BYPASS;
the endpoint itself allows one SELECT and a 3s timeout). The OFFSET is the run slot
(2h windows since the epoch), so each run reads a different org inside every stratum and the
whole range is covered over days, not the same 20 forever.

Usage: audit_sample.py --lane orgs|jobs [--slot N] [--print-sql (orgs)]
Prints JSON lines {"stratum", "ein", "name" | "pid", ...}. A stratum whose query fails prints
{"stratum", "error"} -- an unreadable stratum is a finding, not a skip.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("AUDIT_QUERY_URL", "https://philanthropy.org/990/api/query")

# Viken's set is fixed every run: the holes he reported must stay closed.
VIKEN = ["362193608", "363258696"]  # Shriners, YMCA of the USA; Salvation Army rotates below

_REV = ("SELECT o.ein, o.name FROM organizations o JOIN filings f ON f.ein = o.ein "
        "WHERE {where} GROUP BY o.ein, o.name ORDER BY {order} LIMIT {limit} OFFSET {off}")
# fk5: ORDER BY MAX(f.total_revenue) over the filings join hit the 3s cap every run; the
# organizations.revenue_amt column answers "biggest first" in 0.3s.
_BIG = ("SELECT ein, name FROM organizations WHERE {where} AND revenue_amt IS NOT NULL "
        "ORDER BY revenue_amt DESC LIMIT {limit} OFFSET {off}")
ORG_STRATA = {
    "largest": (_BIG, "TRUE", "", 10, 1),
    "hospital": (_BIG, "ntee_code IN ('E20','E21','E22','E24')", "", 1, 50),
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
# fk6: job_postings is not exposed by /990/api/query (all 6 strata answered 400), so the jobs
# lane reads the PUBLIC pages a job seeker sees, with the same x-atlas-test header.
SITE = os.environ.get("AUDIT_SITE", "https://philanthropy.org")
JOBS_STATE = os.environ.get("AUDIT_JOBS_STATE", "/fleet-kit/state/auditor_jobs_pids.json")
BOT_UA = "fleet-auditor-bot/1"  # a bot UA: the /go hop must not count as a real Apply tap
ATS = ("workday", "greenhouse", "lever", "adp")
_PID = re.compile(r'href="/jobs/j/([0-9a-f]{12})/')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch(path: str, follow: bool = True) -> tuple[int, str, str]:
    """GET a public page: (status, Location header, body). 4xx/5xx are returned, not raised."""
    req = urllib.request.Request(SITE + path, headers={
        "User-Agent": BOT_UA, "x-atlas-test": os.environ.get("ATLAS_TEST_BYPASS", "")})
    opener = urllib.request.build_opener(*([] if follow else [_NoRedirect]))
    try:
        with opener.open(req, timeout=25) as r:
            return r.status, r.headers.get("Location", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), ""


def _page(path: str) -> str:
    status, _, body = fetch(path)
    if status != 200:
        raise RuntimeError(f"{path} answered HTTP {status}")
    return body


def _pids(html: str) -> list[str]:
    return list(dict.fromkeys(_PID.findall(html)))


def _rotate(pids: list[str], slot: int, n: int) -> list[str]:
    if not pids:
        raise RuntimeError("no job links on the page")
    off = (slot * n) % len(pids)
    return (pids[off:] + pids[:off])[:n]


def apply_host(pid: str) -> str:
    """Where Apply goes: the Location of /jobs/j/<pid>/go, read without following it."""
    status, loc, _ = fetch(f"/jobs/j/{pid}/go", follow=False)
    return urllib.parse.urlsplit(loc).hostname or f"HTTP {status}, no Location"


def job_row(pid: str) -> dict:
    """pid, title, employer EIN, apply host and status as the job page shows them. The page's
    JSON-LD carries the pid (identifier) and the org's website (sameAs), never the EIN, so the
    EIN comes from the page's own "All jobs at" link /jobs/at/<ein>/."""
    status, _, html = fetch(f"/jobs/j/{pid}")
    row = {"pid": pid, "status": status}
    if status != 200:
        return row
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    title = json.loads(ld.group(1)).get("title") if ld else None
    h1 = re.search(r"<h1[^>]*>([^<]+)", html)
    ein = re.search(r'href="/jobs/at/(\d{9})/', html)
    return {**row, "title": title or (h1.group(1).strip() if h1 else ""),
            "ein": ein.group(1) if ein else "", "jsonld": bool(ld), "apply_host": apply_host(pid)}


def _highest_paid(slot: int) -> list[str]:
    """The role with the highest median pay in /jobs#pay (/jobs/pay 301s there), then its jobs."""
    html = _page("/jobs").split('id="pay"', 1)[-1]
    best = max(((int(m.group(2).replace(",", "")), m.group(1)) for m in re.finditer(
        r'href="/jobs/pay/([a-z0-9-]+)".*?\$([\d,]+)', html, re.S)), default=None)
    if not best:
        raise RuntimeError("no pay table on /jobs")
    return _rotate(_pids(_page(f"/jobs/pay/{best[1]}")), slot, 3)


def _per_ats(seen: list[str]) -> dict[str, str]:
    """One pid per ATS host, hopping /go on the jobs already seen (at most 40 hops)."""
    found: dict[str, str] = {}
    for pid in seen[:40]:
        host = apply_host(pid)
        for ats in ATS:
            if ats in host and ats not in found:
                found[ats] = pid
        if len(found) == len(ATS):
            break
    return found


def _closed(prev: list[str]) -> list[str]:
    """Jobs seen on an earlier pass whose page now answers 410 Gone."""
    if not prev:
        raise RuntimeError(f"no earlier pass in {JOBS_STATE} yet; closed starts next run")
    gone = [p for p in prev[-30:] if fetch(f"/jobs/j/{p}", follow=False)[0] == 410][:3]
    if not gone:
        raise RuntimeError(f"none of the last {len(prev[-30:])} seen jobs answers 410")
    return gone


def jobs_sample(slot: int) -> list[dict]:
    """The jobs lane's sample: {"stratum", pid, title, ein, apply_host, ...} rows, or
    {"stratum", "error"} for a stratum that could not be read."""
    try:
        prev = json.load(open(JOBS_STATE))
    except (OSError, ValueError):
        prev = []
    out: list[dict] = []
    seen: list[str] = []

    def stratum(name, pick):
        try:
            pids = pick()
            seen.extend(pids)
            out.extend({"stratum": name, **job_row(p)} for p in pids)
        except Exception as e:  # noqa: BLE001 -- an unreadable stratum is reported, not hidden
            out.append({"stratum": name, "error": f"{type(e).__name__}: {e}"[:200]})

    stratum("newest", lambda: _pids(_page("/jobs/all"))[:5])
    stratum("highest_paid", lambda: _highest_paid(slot))
    stratum("leadership", lambda: _rotate(_pids(_page("/jobs/leadership-openings")), slot, 3))
    pool: list[str] = []
    ats: dict[str, str] = {}
    try:
        pool = list(dict.fromkeys(seen + _pids(_page("/jobs/all"))))
        ats = _per_ats(_rotate(pool, slot, len(pool)))
    except Exception as e:  # noqa: BLE001
        out.append({"stratum": "ats", "error": f"{type(e).__name__}: {e}"[:200]})

    def one_ats(name):
        if name not in ats:
            raise RuntimeError(f"no {name} apply link among {len(pool)} jobs")
        return [ats[name]]

    for name in ATS:
        stratum(name, lambda name=name: one_ats(name))
    stratum("closed", lambda: _closed(prev))
    out.append({"stratum": "posted_by_owner", "skip": "owner posts live in HQ, not a public "
                "list; the J4 walk posts one as the QA owner instead"})
    try:
        os.makedirs(os.path.dirname(JOBS_STATE) or ".", exist_ok=True)
        json.dump(list(dict.fromkeys(prev + seen))[-200:], open(JOBS_STATE, "w"))
    except OSError as e:
        out.append({"stratum": "closed", "error": f"state not saved: {e}"[:200]})
    return out


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
    if isinstance(data, dict) and data.get("error"):  # a timeout is a 200 with {"error"}
        raise RuntimeError(data["error"])
    return data.get("rows", data if isinstance(data, list) else [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", choices=("orgs", "jobs"), default="orgs")
    ap.add_argument("--slot", type=int, default=int(time.time() // 7200))
    ap.add_argument("--print-sql", action="store_true")
    a = ap.parse_args(argv)
    if a.lane == "jobs":
        for row in jobs_sample(a.slot):
            print(json.dumps(row))
            if "error" in row:
                print(f"audit_sample: stratum {row['stratum']}: {row['error']}", file=sys.stderr)
        return 0
    for stratum, sql in sql_for(ORG_STRATA, a.slot).items():
        if a.print_sql:
            print(json.dumps({"stratum": stratum, "sql": sql}))
            continue
        try:
            rows = query(sql)
            for row in rows:
                print(json.dumps({"stratum": stratum, **row}))
            if not rows:
                print(json.dumps({"stratum": stratum, "error": "no rows"}))
        except Exception as e:  # noqa: BLE001 -- an unreadable stratum is reported, not hidden
            print(json.dumps({"stratum": stratum, "error": f"{type(e).__name__}: {e}"[:200]}))
            print(f"audit_sample: stratum {stratum} unreadable: {e}"[:300], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
