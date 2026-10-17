#!/usr/bin/env python3
"""auditor lane 990-truth (a1): each check flags the hole Viken found and passes the fixed page.

Fixtures are cut from the real 2026-10-14 Shriners render (commit 79dc379f4): the CSV link and
the PDF link share `/get?fy=2024`, and the footer says "does not rate, score, or rank" next to
graded cards. RED without the fix: scripts/audit_990_truth.py does not exist (ImportError).
Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import sys
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


def test_csv_link_that_serves_the_pdf_is_flagged():
    found = at.csv_link_is_pdf(PDF + CSV_BAD)
    assert found and "same href as the PDF link" in found[0], found
    assert at.csv_link_is_pdf(PDF + CSV_OK) == []


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


def test_schedules_shown_must_match_the_xml():
    page = ('<span class="sch on">A · Public support</span>'
            '<span class="sch on">O · Explanations</span><span class="sch off">J · Pay</span>')
    assert at.schedules_vs_xml(page, XML) == ["filed in the XML, not shown as filed: ['J']"]
    page_ok = page.replace('class="sch off">J', 'class="sch on">J')
    assert at.schedules_vs_xml(page_ok, XML) == []


def test_the_10_14_shriners_shape_trips_both_v1b_and_v1d():
    checks = {f["check"] for f in at.audit(PDF + CSV_BAD + NO_RATE + GRADES, "u")}
    assert {"csv_link_is_pdf", "disclaimer_vs_grades"} <= checks, checks


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
