#!/usr/bin/env python3
"""audit_sample most_visited: the report pages people land on, two orgs a run.

The visits SQL runs here on SQLite, the same text prod's dash store runs (its wrapper turns
json_extract into Postgres). Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_sample as asm  # noqa: E402

UNREADABLE = asm.VISITED_UNREADABLE

SLOT = 246_000  # 2026-02-16 00:00 UTC, list offset 0; the 7-day cut is 2026-02-09 00:00
IN, OLD = "2026-02-15 10:00:00", "2026-02-08 23:00:00"


def _db():
    """dash_events and organizations in one SQLite file: each query names only one of them."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE dash_events (id INTEGER PRIMARY KEY, ts, source, kind, payload);"
                     "CREATE TABLE organizations (ein, name, is_fixture);")
    for ein, name, fx in (("111111111", "Most Seen", 0), ("222222222", "Second", 0),
                          ("333333333", "Third", 0), ("444444444", "Flagged Fixture", 1),
                          ("000000024", "National Disco Ball Foundation", 0)):
        db.execute("INSERT INTO organizations VALUES (?, ?, ?)", (ein, name, fx))

    def view(ein, n, ts=IN, bot=False, kind="viewed"):
        p = {"page": f"/990/report/{ein}", "ein": ein, **({"bot": "ua"} if bot else {})}
        db.executemany("INSERT INTO dash_events (ts, source, kind, payload) VALUES (?, ?, ?, ?)",
                       [(ts, "visitor", kind, json.dumps(p))] * n)

    view("111111111", 5)
    view("222222222", 4)
    view("333333333", 1)
    view("333333333", 9, bot=True)  # bots left out
    view("333333333", 9, ts=OLD)  # older than 7 days
    view("444444444", 3)  # is_fixture: dropped by the name lookup
    view("000000024", 9)  # fixture EIN range: dropped in the visits query
    db.execute("INSERT INTO dash_events (ts, source, kind, payload) VALUES (?, 'visitor', "
               "'viewed', ?)", (IN, json.dumps({"page": "/990/search"})))
    return lambda sql: [dict(r) for r in db.execute(sql)]


def _run(query, slot=SLOT):
    real, asm.query, asm.VISITED_UNREADABLE = asm.query, query, ""
    try:
        return asm.most_visited(slot)
    finally:
        asm.query, asm.VISITED_UNREADABLE = real, UNREADABLE


def test_while_no_query_fits_the_cap_the_stratum_is_an_error_row_and_sends_nothing():
    sent = []
    rows = _main(sent.append)
    assert not any("dash_events" in s for s in sent), sent
    assert rows == [{"stratum": "most_visited",
                     "error": "RuntimeError: " + asm.VISITED_UNREADABLE}], rows


def test_visits_sql_ranks_human_report_views_over_seven_days():
    sql = asm.most_visited_sql(SLOT)
    assert "ts > '2026-02-09 00:00:00'" in sql, sql
    rows = _db()(sql.replace("LIMIT 2", "LIMIT 10"))
    assert rows == [{"ein": "111111111", "visits": 5}, {"ein": "222222222", "visits": 4},
                    {"ein": "444444444", "visits": 3}, {"ein": "333333333", "visits": 1}], rows


def test_the_slot_walks_down_the_list_two_at_a_time_and_wraps():
    q = _db()
    runs = asm.VISITED_SPAN // asm.VISITED_LIMIT  # 50 runs cover the top 100
    assert "OFFSET 0" in asm.most_visited_sql(SLOT)
    assert "OFFSET 2" in asm.most_visited_sql(SLOT + 1)
    assert "OFFSET 0" in asm.most_visited_sql(SLOT + runs)
    assert [r["ein"] for r in _run(q, SLOT)] == ["111111111", "222222222"]
    # offset 2 holds the is_fixture org and Third; only Third is a real org
    assert _run(q, SLOT + 1) == [{"stratum": "most_visited", "ein": "333333333",
                                  "name": "Third", "visits": 1}]


def _main(query, unreadable=None):
    real, asm.query = asm.query, query
    if unreadable is not None:
        asm.VISITED_UNREADABLE = unreadable
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            asm.main(["--lane", "orgs", "--slot", str(SLOT)])
    finally:
        asm.query, asm.VISITED_UNREADABLE = real, UNREADABLE
    return [json.loads(line) for line in out.getvalue().splitlines()
            if json.loads(line)["stratum"] == "most_visited"]


def test_a_failed_or_slow_visits_query_prints_an_error_row():
    def fail(sql):
        raise RuntimeError("query exceeded the 3s time limit")
    assert _main(fail, unreadable="") == [{"stratum": "most_visited",
                            "error": "RuntimeError: query exceeded the 3s time limit"}]
    clock = iter([0.0, 3.5])
    real = asm.time.monotonic
    asm.time.monotonic = lambda: next(clock)
    try:
        rows = _main(lambda sql: [{"ein": "111111111", "visits": 5}], unreadable="")
    finally:
        asm.time.monotonic = real
    assert rows == [{"stratum": "most_visited",
                     "error": "RuntimeError: visits query took 3.5s, over the 3s cap"}], rows


def test_print_sql_lists_the_visits_query():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asm.main(["--lane", "orgs", "--slot", str(SLOT), "--print-sql"])
    rows = {json.loads(line)["stratum"]: json.loads(line)["sql"] for line in out.getvalue().splitlines()}
    assert rows["most_visited"] == asm.most_visited_sql(SLOT)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")


def test_every_org_sample_draws_one_filer_with_negative_total_assets():
    # #13444: the ratio tests broke only on negative-asset filers, which no random draw hit.
    sql = asm.sql_for(asm.ORG_STRATA, 7)["negative_assets"]
    assert "f.total_assets_eoy < 0" in sql and "LIMIT 1 " in sql
