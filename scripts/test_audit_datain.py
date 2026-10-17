#!/usr/bin/env python3
"""auditor data-in lane (fk8): the slug, the sample and D2/D4/D5/D6 run on what the lane can
reach, and a check that cannot run says so. RED without the fix: --repo /repo went to gh as is,
the sample could return fixture EIN 000000024, and audit_datain.py did not exist.
Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_datain as ad  # noqa: E402
import audit_file as af  # noqa: E402
import audit_sample as asm  # noqa: E402


def test_a_checkout_path_resolves_to_the_repo_slug():
    asked = []
    gh = lambda args: asked.append(args) or "PhilanthropyOrg/philanthropy\n"  # noqa: E731
    assert af.repo_slug("/repo", gh) == "PhilanthropyOrg/philanthropy"
    assert af.repo_slug("", gh) == "PhilanthropyOrg/philanthropy"
    assert asked[0][:3] == ["repo", "view", "--json"], asked
    assert af.repo_slug("PhilanthropyOrg/fleet-kit", gh) == "PhilanthropyOrg/fleet-kit"
    assert len(asked) == 2  # a real slug is used as is


def test_every_org_stratum_leaves_out_fixture_orgs():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE organizations (ein, name, ntee_code, revenue_amt, is_fixture);"
        "CREATE TABLE filings (ein, tax_year, total_revenue);"
        "CREATE TABLE filing_index (object_id, ein, return_type, tax_period);")
    for ein, name, fx in (("000000024", "National Disco Ball Foundation", 0),
                          ("123456789", "SALVATION ARMY TEST FIXTURE", 1)):
        db.execute("INSERT INTO organizations VALUES (?, ?, 'E20', 9e12, ?)", (ein, name, fx))
        db.execute("INSERT INTO filings VALUES (?, 2023, 100)", (ein,))
        db.execute("INSERT INTO filing_index VALUES (9, ?, '990PF', '202312')", (ein,))
    db.execute("UPDATE organizations SET ntee_code = 'X20' WHERE ein = '000000024'")
    for name, sql in asm.sql_for(asm.ORG_STRATA, 0).items():
        assert "is_fixture IS NOT TRUE" in sql and "NOT LIKE '0000000%'" in sql, name
        assert db.execute(sql).fetchall() == [], (name, sql)


def _d2_db(index, filings):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE filing_index (object_id, ein, tax_period);"
                     "CREATE TABLE filings (ein, tax_year);")
    db.executemany("INSERT INTO filing_index VALUES (?, ?, ?)", index)
    db.executemany("INSERT INTO filings VALUES (?, ?)", filings)
    return lambda sql: [dict(r) for r in db.execute(sql)]


def test_d2_reads_tax_period_and_derives_the_tax_year():
    assert "tax_year" not in ad.D2_SQL.split("filings f")[0], ad.D2_SQL  # the index has no tax_year
    q = _d2_db([(1, "111111111", "202312"), (2, "222222222", "202306"), (3, "333333333", "202406")],
               [("111111111", 2023), ("222222222", 2022)])
    out = ad.d2(q)
    assert len(out) == 1 and "1 of the newest" in out[0] and "333333333 202406" in out[0], out
    assert ad.d2(_d2_db([(1, "111111111", "202312")], [("111111111", 2023)])) == []


def test_d4_scans_public_json_values_one_at_a_time_and_the_report_page():
    facts = {"city": "TAMPA", "officers": [{"name": "Ann Lee", "address": "12 Maple Street"}],
             "a": "Suite 174", "b": "Occupancy Ln 16", "mail": "ann.lee@gmail.com"}
    table = b"<td>174</td><td>Occupancy</td><td>Ln 16</td>"
    pages = {f"{ad.SITE}/990/api/org/1/facts": (200, "json", json.dumps(facts).encode()),
             f"{ad.SITE}/990/report/1": (200, "text/html", table),
             f"{ad.SITE}/990/api/org/2/facts": (404, "", b""),
             f"{ad.SITE}/990/report/2": (200, "text/html", b"<p>Tampa, FL</p>")}
    rows = ad.d4(["1", "2"], get=pages.__getitem__)
    found = " ".join(r["finding"] for r in rows if "finding" in r)
    assert "12 Maple Street" in found and "ann.lee@gmail.com" in found, rows
    assert "Occupancy" not in found and "/990/report/1" not in found, rows
    assert [r["error"] for r in rows if "error" in r] == [
        f"{ad.SITE}/990/api/org/2/facts answered HTTP 404"], rows


def _day(d, hit, uncounted=0):
    return {"d": d, "crawler_hit": hit, "uncounted": uncounted}


def test_d5_reads_the_superadmin_crawler_flag():
    doc = {"jobs": {"real": [_day("2026-10-14", False), _day("2026-10-15", True, 1417)]}}
    assert ad.d5(lambda: doc) == [
        "2026-10-15: 1417 Apply hits came from no-browser visitors (10x or more the real ones); "
        "a count that includes them is inflated"]
    rows = ad.run(["d5"], [], {"d5": lambda: ad.d5(lambda: {"jobs": {"real": None},
                                                            "errors": {"jobs.real": "timeout"}})})
    assert rows == [{"check": "d5", "error": "RuntimeError: kpi jobs.real is empty: timeout"}], rows


def test_d6_flags_a_search_console_feed_that_stopped():
    now = dt.datetime(2026, 10, 17, 9)
    assert "2026-10-14, 3 days old" in ad.d6(lambda sql: [{"m": "2026-10-14 00:00:00"}], now)[0]
    assert ad.d6(lambda sql: [{"m": "2026-10-16 23:00:00"}], now) == []


def test_a_check_that_cannot_run_is_loud_and_fails_the_run():
    import contextlib
    import io

    def down():
        raise ConnectionError("HTTP 403")

    real, ad.run = ad.run, lambda names, eins: real(names, eins, {"d2": down, "d6": lambda: []})
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = ad.main(["d2", "d6"])
    finally:
        ad.run = real
    assert rc == 2 and "CHECK d2 DID NOT RUN" in err.getvalue(), err.getvalue()
    assert real(["d6"], [], {"d6": lambda: []}) == [{"check": "d6", "ok": True}]
    try:
        ad.d4([])
    except RuntimeError as e:
        assert "--sample" in str(e)
    else:
        raise AssertionError("d4 with no sample must not pass")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
