#!/usr/bin/env python3
"""jobs_daily.py -- the Jobs app reads its own numbers every morning and files what broke.

Reif, 2026-10-14, on the Jobs self-improvement loop: "anything weekly, for self improvement,
should be daily". The product serves the Jobs counts in its KPI document (GET
/api/signals/kpi, key `jobs`; kpi_jobs.py in the product repo). This pulls that document
once a day, keeps the day's copy, compares it with the last one, and

  - posts the day's report as a comment on the Jobs epic (#6772), so anyone watching the
    epic sees the numbers move, and
  - files one `bug` issue per rule that trips (fleet:backlog + quality:solid + bug, through
    issue_cluster.file_issue, so a repeat lands as a comment on the open twin, never a
    second issue). The fixer lane builds bugs; that is the loop.

No model. Every rule is a plain comparison in rules() that anyone can read.

  jobs_daily.py            -> fetch, save, compare, comment on the epic, file findings
  jobs_daily.py --dry-run  -> fetch, save, print the report; comment on nothing, file nothing

Env: FLEET_NUMBER_URL (…/api/number; the base is derived from it), FLEET_NUMBER_TOKEN,
FLEET_LOG_DIR. gh runs in the cwd's repo, so the cron does `cd $FLEET_REPO` first.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fleet_tz import stamp as central_stamp  # noqa: E402 -- humans read Central

LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
SIG_DIR = LOG_DIR / "signals"
EPIC = int(os.environ.get("FLEET_JOBS_EPIC") or 6772)
LABELS = ["fleet:backlog", "quality:solid", "bug"]
WHO = "jobs_daily"
GATE = (
    "Vision-link: okr.traffic\n"
    "Strategy: sauce\n"
    "Best in class: Indeed never shows a stale or empty board; a person sees today's jobs "
    "with the pay next to them."
)


class Finding(NamedTuple):
    title: str
    body: str


# --- the read ---------------------------------------------------------------------------


def base_url() -> str:
    url = (os.environ.get("FLEET_NUMBER_URL") or "").strip()
    return url[: url.rfind("/api/")] if "/api/" in url else ""


def fetch_kpi(timeout: float = 90.0) -> dict:
    token = os.environ.get("FLEET_NUMBER_TOKEN", "").strip()
    req = urllib.request.Request(
        base_url() + "/api/signals/kpi?fresh=1",
        headers={"X-PM-Token": token, "User-Agent": "fleet-kit/jobs_daily"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def save(doc: dict, day: str) -> pathlib.Path:
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    p = SIG_DIR / f"jobs-{day}.json"
    p.write_text(json.dumps(doc, indent=1))
    return p


def previous(day: str) -> dict | None:
    try:
        days = sorted(q for q in SIG_DIR.glob("jobs-*.json") if q.stem < f"jobs-{day}")
    except OSError:
        return None
    for q in reversed(days):
        try:
            return json.loads(q.read_text())
        except (OSError, ValueError):
            continue
    return None


# --- the rules --------------------------------------------------------------------------


def _jobs(doc: dict | None) -> dict:
    return ((doc or {}).get("jobs") or {}) if isinstance(doc, dict) else {}


def numbers(doc: dict | None) -> dict:
    """The flat numbers a report row reads; None where the document has none."""
    j = _jobs(doc)
    p, b, y, s = (j.get(k) or {} for k in ("postings", "boards", "pay", "posted"))
    series = p.get("daily_30d") or []
    last = series[-1] if series else {}
    proof = b.get("by_proof") or {}
    places = y.get("pages_by_place") or {}
    return {
        "open jobs": p.get("open"),
        "orgs hiring": p.get("orgs_hiring"),
        "open jobs with a role page": p.get("open_with_role"),
        "roles with open jobs": p.get("roles_with_open_jobs"),
        "new today": last.get("new") if series else None,
        "closed today": last.get("closed") if series else None,
        "boards": b.get("total"),
        "boards by site link": proof.get("site-link"),
        "boards by name": proof.get("name"),
        "jobs on boards": b.get("jobs_on_boards"),
        "pay pages": sum(places.values()) if places else None,
        "pay pages by state": places.get("state"),
        "titles with pay": y.get("titles"),
        "posted listings": sum((s.get("by_status") or {}).values()) if s.get("by_status") else None,
        "posted drafts": (s.get("by_status") or {}).get("draft"),
        "posted open": (s.get("by_status") or {}).get("open"),
        "pages that can index": j.get("pages_indexable"),
    }


def _body(what: str, gwt: str, today: dict | None, prev: dict | None) -> str:
    n = numbers(today)
    m = numbers(prev)
    rows = "\n".join(f"- {k}: {n[k]} (yesterday {m[k]})" for k in n)
    return (
        f"{what}\n\nFound by the daily Jobs check (fleet-kit scripts/jobs_daily.py), reading "
        f"/api/signals/kpi `jobs` at {central_stamp()}. Crons on the box: jobs_resolve_ats "
        f"(boards), jobs_harvest_postings (open jobs), jobs_build_pay_cells (pay pages); logs in "
        f"/var/log/atlas/. docs/ops/crons.md has each one.\n\n{GATE}\n\n{gwt}\n\nNumbers:\n{rows}\n"
    )


def rules(today: dict | None, prev: dict | None) -> list[Finding]:
    """One Finding per rule that trips. Titles carry no numbers, so a repeat on the next
    day dedupes onto the same open issue (issue_cluster matches on the title)."""
    out: list[Finding] = []
    errs = {k: v for k, v in ((today or {}).get("errors") or {}).items() if k.startswith("jobs")}
    j = _jobs(today)
    if not j or errs:
        out.append(
            Finding(
                "Jobs KPI section failed",
                _body(
                    f"/api/signals/kpi has no usable `jobs` section. errors: {errs or 'section missing'}",
                    "Given the KPI document, When the superadmin dashboard polls it, Then `jobs` "
                    "has numbers and `errors` has no jobs key.",
                    today,
                    prev,
                ),
            )
        )
        return out
    n, m = numbers(today), numbers(prev)
    if n["open jobs"] is None:
        out.append(
            Finding(
                "Jobs tables are missing on prod",
                _body(
                    "The `jobs` section is all nulls: job_postings, ats_map or jobs_pay_cells is not there.",
                    "Given the Jobs crons ran, When /jobs loads, Then it lists open jobs and the KPI "
                    "document counts them.",
                    today,
                    prev,
                ),
            )
        )
        return out
    if prev and m["open jobs"] == n["open jobs"] and not n["new today"] and not n["closed today"]:
        out.append(
            Finding(
                "Jobs harvest did not run in the last day",
                _body(
                    "Open jobs did not move since yesterday and today's row has no new and no closed "
                    "job. jobs_harvest_postings (10:40 UTC) did not run, or ran and saved nothing.",
                    "Given the harvest cron, When a day passes, Then the KPI daily series shows new or "
                    "closed jobs for that day and /var/log/atlas/jobs_harvest_postings.log has the run.",
                    today,
                    prev,
                ),
            )
        )
    if prev and m["boards"] and (n["boards"] or 0) < 0.75 * m["boards"]:
        out.append(
            Finding(
                "Jobs boards fell by more than a quarter in a day",
                _body(
                    f"ats_map went from {m['boards']} boards to {n['boards']}. The resolver's 50% guard "
                    "did not catch it; a proof rule or a vendor API changed.",
                    "Given yesterday's boards, When jobs_resolve_ats runs, Then no proved board drops "
                    "unless its org's site stopped linking it or the board is gone.",
                    today,
                    prev,
                ),
            )
        )
    if prev and m["open jobs"] and (n["open jobs"] or 0) < 0.7 * m["open jobs"]:
        out.append(
            Finding(
                "Open jobs fell by more than a third in a day",
                _body(
                    f"Open jobs went from {m['open jobs']} to {n['open jobs']}.",
                    "Given yesterday's open jobs, When the harvest runs, Then a board that failed to "
                    "read closes nothing (fetch_board returns None) and only jobs gone from their "
                    "board close.",
                    today,
                    prev,
                ),
            )
        )
    if n["open jobs"] and (n["open jobs with a role page"] or 0) < 0.5 * n["open jobs"]:
        out.append(
            Finding(
                "Half of the open jobs have no role page",
                _body(
                    f"{n['open jobs with a role page']} of {n['open jobs']} open jobs carry a title "
                    "slug. The title matcher (job_titles.match) misses the rest, so they are on no "
                    "/jobs/{role} page and no pay page.",
                    "Given the open jobs, When their titles run through the matcher, Then at least "
                    "half land on a role page; the unmatched titles are listed in the issue.",
                    today,
                    prev,
                ),
            )
        )
    if not n["pay pages"] or not n["pay pages by state"]:
        out.append(
            Finding(
                "Jobs pay pages are empty",
                _body(
                    f"jobs_pay_cells has {n['titles with pay']} titles and {n['pay pages']} plain pages; "
                    f"state pages: {n['pay pages by state']}. jobs_build_pay_cells (Sunday 11:20 UTC) "
                    "did not run or wrote nothing.",
                    "Given the 990 Part VII rows, When the pay build runs, Then /jobs/pay/{role} and "
                    "/jobs/pay/{role}/in/{state} have numbers for every role with 10+ orgs.",
                    today,
                    prev,
                ),
            )
        )
    return out


# --- the report -------------------------------------------------------------------------


def render(today: dict | None, prev: dict | None, findings: list[Finding], filed: list[str]) -> str:
    n, m = numbers(today), numbers(prev)
    lines = [
        f"## Jobs, daily check, {central_stamp('%Y-%m-%d %-I:%M %p %Z')}",
        "",
        "| | today | yesterday |",
        "|---|---:|---:|",
    ]
    for k in n:
        lines.append(f"| {k} | {_fmt(n[k])} | {_fmt(m[k])} |")
    lines.append("")
    if findings:
        lines.append("Filed:" if filed else "Would file:")
        for f, ref in zip(findings, filed or [""] * len(findings)):
            lines.append(f"- {f.title}{f' ({ref})' if ref else ''}")
    else:
        lines.append("Nothing to file: every rule passed.")
    lines.append("")
    lines.append("Rules and source: fleet-kit scripts/jobs_daily.py. Numbers: /api/signals/kpi `jobs`.")
    return "\n".join(lines)


def _fmt(v) -> str:
    return "–" if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def comment_on_epic(text: str, run=subprocess.run) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        r = run(["gh", "issue", "comment", str(EPIC), "--body-file", path],
                capture_output=True, text=True)
    finally:
        os.unlink(path)
    if r.returncode != 0:
        print(f"jobs_daily: comment on #{EPIC} FAILED: {(r.stderr or r.stdout)[:300]}", file=sys.stderr)
    return r.returncode == 0


def file_findings(findings: list[Finding]) -> list[str]:
    import issue_cluster

    refs = []
    for f in findings:
        res = issue_cluster.file_issue(f.title, f.body, LABELS, who=WHO)
        if not res.get("ok"):
            print(f"jobs_daily: file FAILED for {f.title!r}: {res.get('error', '')[:300]}", file=sys.stderr)
            refs.append("not filed")
        elif res.get("action") == "commented":
            refs.append(f"#{res['number']}, seen again")
        else:
            refs.append(res.get("url") or "filed")
    return refs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dry-run", action="store_true", help="print the report; comment and file nothing")
    args = ap.parse_args(argv)
    if not base_url():
        print("jobs_daily: FLEET_NUMBER_URL unset or has no /api/ segment", file=sys.stderr)
        return 1
    day = day_of(time.time())
    prev = previous(day)
    try:
        today = fetch_kpi()
    except Exception as exc:  # noqa: BLE001 -- a failed read is a finding, not a crash
        today = {"errors": {"jobs.fetch": f"{type(exc).__name__}: {str(exc)[:160]}"}}
    save(today, day)
    findings = rules(today, prev)
    if args.dry_run:
        print(render(today, prev, findings, []))
        for f in findings:
            print(f"\n--- would file: {f.title}\n{f.body}")
        return 0
    filed = file_findings(findings)
    text = render(today, prev, findings, filed)
    print(text)
    comment_on_epic(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
