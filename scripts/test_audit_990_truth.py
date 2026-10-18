#!/usr/bin/env python3
"""auditor lane 990-truth (a1): each check flags the hole Viken found and passes the fixed page.

Fixtures are cut from the real 2026-10-14 Shriners render (commit 79dc379f4): the CSV link and
the PDF link share `/get?fy=2024`, and the footer says "does not rate, score, or rank" next to
graded cards. On 10-14 that link served the PDF bytes (_SERVED). The CSV check is judged by
what a link serves after redirects (fk7), so the 10-17 pass's /get?fy=..&fmt=csv -> text/csv is
clean, and the street check reads one text node at a time ("174" + "Occupancy" + "Ln 16" are
three cells of an expense table, not an address). Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_990_truth as at  # noqa: E402

PDF = '<a href="/990/report/362193608/get?fy=2024">990 PDF</a>'
CSV_BAD = ('<a role="menuitem" href="/990/report/362193608/get?fy=2024"><span class="num">CSV'
           '</span><span><b>990 data</b></span></a>')
CSV_OK = '<a href="/990/report/362193608/data.csv?fy=2024"><span>CSV</span> 990 data</a>'
GRADES = '<span class="g">Looks good</span><span class="g">Concern</span>'
NO_RATE = "<p>philanthropy.org does not rate, score, or rank organizations.</p>"
XML = ('<Return xmlns="http://www.irs.gov/efile"><ReturnData><IRS990/><IRS990ScheduleA/>'
       "<IRS990ScheduleJ/><IRS990ScheduleO/></ReturnData></Return>")
_SERVED = {"/990/report/362193608/get?fy=2024": (200, "application/pdf", b"%PDF-1.7", "/x.pdf"),
           "/990/report/362193608/data.csv?fy=2024": (200, "text/csv", b"ein,fy\n", "/x.csv")}
_GET_PAGE = (200, "text/html; charset=utf-8", b"<!doctype html>",
             "https://h/990/report/1/get?fy=2024")


def _evidence(page, fetch, signed_in=None):
    found, gated = at.download_hits(page, "u", fetch, signed_in)
    return [f["evidence"] for f in found], gated


def test_csv_link_that_serves_the_pdf_is_flagged():
    found, _ = _evidence(PDF + CSV_BAD, _SERVED.get)
    assert found == ['"CSV 990 data" -> /990/report/362193608/get?fy=2024 serves a PDF'], found
    assert _evidence(PDF + CSV_OK, _SERVED.get) == ([], 0)


def test_a_link_that_lands_on_the_get_page_or_sign_in_is_gated_not_a_wrong_file():
    """fk10: ~210 of ~260 hits a pass were links landing on /get or /990/login."""
    get = '<a href="/990/report/1/get">Get the full report (PDF)</a>'
    assert _evidence(get, lambda h: _GET_PAGE) == ([], 1)
    login = (200, "text/html", b"<html>", "https://h/990/login?next=/x")
    assert _evidence('<a href="/x">Get the PDF</a>', lambda h: login) == ([], 1)
    # a label that promised the file itself: one pattern per page, with the count
    page = CSV_OK.replace("data.csv?fy=2024", "get") + CSV_BAD
    found, gated = _evidence(page, lambda h: _GET_PAGE)
    assert gated == 2 and len(found) == 1, found
    assert found[0].startswith("CSV/PDF links land on a gate: 2 (see #12783)"), found


def test_signed_in_follows_the_gate_once_and_judges_the_final_bytes():
    pdf_bytes = (200, "application/octet-stream", b"%PDF-1.7", "https://h/990/x.pdf")
    csv_bytes = (200, "text/csv", b"ein,fy\n", "https://h/990/x.csv")
    assert _evidence(CSV_BAD, lambda h: _GET_PAGE, lambda h: pdf_bytes)[0] == [
        '"CSV 990 data" -> /990/report/362193608/get?fy=2024 serves a PDF']
    assert _evidence(CSV_BAD, lambda h: _GET_PAGE, lambda h: csv_bytes) == ([], 0)


class _Site(BaseHTTPRequestHandler):
    """/get?..&fmt=csv 302s to a CSV; /get?fy=2024 302s to PDF bytes sent as octet-stream."""
    ROUTES = {"/r/get?fy=2024&fmt=csv": (302, "/r/export.csv"),
              "/r/export.csv": (200, "text/csv", b"ein,fy,total\n362193608,2024,1\n"),
              "/r/get?fy=2024": (302, "/r/990.pdf"),
              "/r/990.pdf": (200, "application/octet-stream", b"%PDF-1.7 ...")}

    def do_GET(self):
        r = self.ROUTES[self.path]
        self.send_response(r[0])
        if r[0] == 302:
            self.send_header("Location", r[1])
            self.end_headers()
            return
        self.send_header("Content-Type", r[1])
        self.end_headers()
        self.wfile.write(r[2])

    def log_message(self, *a):
        pass


def test_csv_check_follows_redirects_and_reads_the_bytes():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        fetch = at.fetcher(f"http://127.0.0.1:{srv.server_port}/r")
        pdf = '<a href="/r/get?fy=2024">990 PDF</a>'
        ok = '<a href="/r/get?fy=2024&amp;fmt=csv"><span>CSV</span> 990 data</a>'
        assert _evidence(pdf + ok, fetch) == ([], 0)
        bad, _ = _evidence(pdf + '<a href="/r/get?fy=2024">CSV 990 data</a>', fetch)
        assert bad == ['"CSV 990 data" -> /r/get?fy=2024 serves a PDF'], bad
    finally:
        srv.shutdown()


def test_disclaimer_that_contradicts_grades_is_flagged():
    found = at.disclaimer_vs_grades(NO_RATE + GRADES)
    assert found and "2 grade words" in found[0], found
    assert at.disclaimer_vs_grades("<p>We compare with similar orgs.</p>" + GRADES) == []
    assert at.disclaimer_vs_grades(NO_RATE) == []


def test_names_rows_and_privacy():
    people = ('<a href="/990/person/1">k h</a><a href="/990/person/2">JOHN SMITH</a>'
              '<a href="/990/person/3">Ron Devoll</a>')
    assert at.garbage_names(people) == ["initials only: 'k h'", "ALL CAPS: 'JOHN SMITH'"]
    assert at.hidden_rows("<button>+38 more</button>") == ["+38 more"]
    assert at.hidden_rows("<button>+2 more</button>") == []
    leaks = at.privacy_leaks("<p>2900 Rocky Point Dr, Tampa FL</p><p>ann@gmail.com</p>")
    assert len(leaks) == 2, leaks
    assert at.privacy_leaks("<p>Tampa, FL</p>") == []


def test_expense_table_cells_are_not_a_street_address():
    row = ('<tr><td>$174</td><th><span class="bn-l">Occupancy</span>'
           '<span class="bn-ln">Ln 16</span></th></tr>')
    assert at.privacy_leaks(row) == [], at.privacy_leaks(row)
    assert at.privacy_leaks("<p>174 Occupancy Ln 16</p>") == []
    assert at.privacy_leaks("<p>174 Maple Ln, Tampa FL</p>") == ["street address: '174 Maple Ln'"]


def test_schedules_shown_must_match_the_xml():
    page = ('<span class="sch on">A · Public support</span>'
            '<span class="sch on">O · Explanations</span><span class="sch off">J · Pay</span>')
    assert at.schedules_vs_xml(page, XML) == ["filed in the XML, not shown as filed: ['J']"]
    page_ok = page.replace('class="sch off">J', 'class="sch on">J')
    assert at.schedules_vs_xml(page_ok, XML) == []


def test_a_form_part_lit_under_schedules_filed_is_flagged():
    """#12982: "III · Programs" sat lit beside R and I on every full 990; T3 had exempted it."""
    page = '<span class="sch on">III · Programs</span><span class="sch on">I · Grants</span>'
    assert at.schedule_letters(page) == ["not an IRS schedule, shown as filed: 'III'"]
    assert at.schedule_letters(page.split("</span>", 1)[1]) == []


def _tile(name, big, unit, why):
    return (f'<div class="byg-tile byg-good" data-tile="{name}" data-tone="good" title="x">'
            f'<div class="byg-q">Q</div><div class="byg-big num">{big}</div>'
            f'<div class="byg-unit num">{unit}</div><div class="byg-why">{why}</div>'
            '<div class="byg-cite num">Pt X</div></div>')


def test_a_tile_and_its_reason_line_print_the_same_figure():
    """#12981, the 10-17 Fidelity render: 0.5% over 0%, 4 mo over 3.5 months."""
    page = (_tile("debt", "$407.6M", "0.5% of total assets",
                  "Looks good: liabilities are 0% of assets; 50% or less.")
            + _tile("reserves", "4 mo", "of spending, in cash",
                    "Looks good: 3.5 months of spending in cash; 3 or more."))
    assert [e.split(":")[0] for e in at.tile_vs_reason(page)] == ["tile debt", "tile reserves"]
    ok = (_tile("debt", "$407.6M", "0.5% of total assets",
                "Looks good: liabilities are 0.5% of assets; 50% or less.")
          + _tile("programs", "100%", "of spending",
                  "Looks good: 100% went to programs; 75% or more."))
    assert at.tile_vs_reason(ok) == []


def test_the_programs_tile_and_the_fact_box_print_the_same_share():
    """#13131, Memphis Goodwill live 10-18: tile 99%, fact box 100 cents of each $1."""
    fact = ('<div class="rp-fact"><svg></svg><span><b class="num">{}\u00a2</b> of each '
            '<b class="num">$1</b> spent went to programs.</span></div>')
    tile = _tile("programs", "99%", "of spending", "Looks good: 99% went to programs.")
    assert [e.split(":")[0] for e in at.tile_vs_fact(tile + fact.format(100))] == [
        "tile programs"]
    assert at.tile_vs_fact(tile + fact.format(99)) == []


def test_the_10_14_shriners_shape_trips_both_v1b_and_v1d():
    page = PDF + CSV_BAD + NO_RATE + GRADES
    found = at.audit(page, "u") + at.download_hits(page, "u", _SERVED.get)[0]
    checks = {f["check"] for f in found}
    assert {"csv_link_is_pdf", "disclaimer_vs_grades"} <= checks, checks


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
