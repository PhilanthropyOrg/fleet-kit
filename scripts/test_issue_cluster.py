#!/usr/bin/env python3
"""Levers 2 + 3 (2026-09-24): mega issues and dedupe-at-birth share one key, issue_cluster.signature.

Fixture = real open-board shapes from PhilanthropyOrg/philanthropy that day: seven
`prod alert [pg_lock:<pid>:pg_toast_16627]` twins, one pg_lock on a DIFFERENT relation (a
different root cause that must never be folded in), a per-member "X's reports have no
plain-English words" family with one Reif ask in it, and a claimed twin (in flight, untouchable).

RED without the fix: scripts/issue_cluster.py does not exist (ImportError). GREEN with it.
Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import issue_cluster as ic  # noqa: E402


def L(*names):
    return [{"name": n} for n in names]


FIXTURE = [
    *[{"number": n, "title": f"prod alert [pg_lock:{pid}:pg_toast_16627]", "body": "",
       "labels": L("fleet:backlog", "lane:devops", "fleet:priority-low", "fleet:complexity-2")}
      for n, pid in [(7236, 1549162), (7358, 2733161), (7360, 2744423), (7368, 2774078)]],
    {"number": 6969, "title": "prod alert [pg_lock:2076702:idx_users_email]", "body": "",
     "labels": L("fleet:backlog", "lane:devops")},
    {"number": 7471, "title": "gru's reports have no plain-English words (12 of 12 finished runs in 24h)",
     "body": "", "labels": L("fleet:backlog", "lane:fleet", "fleet:priority-high", "fleet:complexity-3")},
    {"number": 7472, "title": "judge-judy's reports have no plain-English words (69 of 69 finished runs in 24h)",
     "body": "", "labels": L("fleet:backlog", "lane:fleet", "fleet:complexity-4")},
    {"number": 7473, "title": "marie's reports have no plain-English words (3 of 3 finished runs in 24h)",
     "body": "", "labels": L("fleet:backlog", "lane:fleet")},
    {"number": 7474, "title": "minion's reports have no plain-English words (21 of 21 finished runs in 24h)",
     "body": "", "labels": L("fleet:backlog", "lane:fleet", "fleet:reif-asked")},
    {"number": 7475, "title": "nerd's reports have no plain-English words (20 of 20 finished runs in 24h)",
     "body": "", "labels": L("fleet:backlog", "lane:fleet", "fleet:claimed")},
    # same words, different lane: a different problem
    {"number": 7600, "title": "sentry's reports have no plain-English words (4 of 4 finished runs in 24h)",
     "body": "", "labels": L("fleet:backlog", "lane:ui")},
]


def by_sig(plans):
    return {p["signature"]: p for p in plans}


def test_signature_collapses_firings_not_causes() -> None:
    a = ic.signature("prod alert [pg_lock:3973044:pg_toast_16627]")
    b = ic.signature("prod alert [pg_lock:3968749:pg_toast_16627]")
    c = ic.signature("prod alert [pg_lock:2076702:idx_users_email]")
    assert a == b, (a, b)
    assert a != c, "a lock on a different relation is a different root cause"
    assert ic.signature("gru's reports have no plain-English words (12 of 12 finished runs in 24h)") == \
        ic.signature("vp's reports have no plain-English words (15 of 15 finished runs in 24h)")
    print("ok  signature: firings of one failure collapse, different causes stay apart")


def test_plan_never_merges_across_root_causes() -> None:
    plans = by_sig(ic.plan_megas(FIXTURE, min_size=3))
    pg = plans[ic.signature("prod alert [pg_lock:1:pg_toast_16627]")]
    assert [c["number"] for c in pg["children"]] == [7236, 7358, 7360, 7368], pg["children"]
    for p in plans.values():
        sigs = {ic.signature(c["title"]) for c in p["children"] + p["linked"]}
        lanes = {ic.lane(c) for c in p["children"] + p["linked"]}
        assert len(sigs) == 1 and len(lanes) == 1, f"plan mixes causes: {sigs} {lanes}"
    assert all(6969 not in [c["number"] for c in p["children"]] for p in plans.values()), \
        "idx_users_email lock folded into the pg_toast mega"
    print("ok  plan_megas: one signature + one lane per mega, a different lock target stays out")


def test_reif_asks_linked_never_closed_and_claimed_untouched() -> None:
    plans = by_sig(ic.plan_megas(FIXTURE, min_size=3))
    rep = plans[ic.signature("gru's reports have no plain-English words (1 of 1 finished runs in 24h)")]
    kids = [c["number"] for c in rep["children"]]
    assert kids == [7471, 7472, 7473], kids
    assert [c["number"] for c in rep["linked"]] == [7474], rep["linked"]
    assert 7475 not in kids, "a claimed (in-flight) item was folded"
    assert 7600 not in kids, "a different lane was folded"

    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/issues/9001\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = ic.apply_plan(rep, run=run)
    assert res["ok"] and res["mega"] == 9001 and res["closed"] == [7471, 7472, 7473], res
    closes = [c for c in calls if c[:3] == ["gh", "issue", "close"]]
    assert all(c[3] != "7474" for c in closes), "a Reif ask was closed"
    assert calls[0][:3] == ["gh", "issue", "create"], "children closed before the mega existed"
    create = calls[0]
    body = create[create.index("--body") + 1]
    assert "- [ ] #7471" in body and "#7474" in body and ic.SIG_MARKER in body, body
    labels = [create[i + 1] for i, x in enumerate(create) if x == "--label"]
    assert ic.MEGA_LABEL in labels and "fleet:priority-high" in labels and "fleet:complexity-4" in labels, labels
    assert any("Tracked in #9001" in " ".join(c) for c in closes)
    print("ok  apply_plan: mega first, children closed 'tracked in #M', Reif ask linked and left open")


def test_failed_mega_create_closes_nothing() -> None:
    plan = ic.plan_megas(FIXTURE, min_size=3)[0]
    calls = []

    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    res = ic.apply_plan(plan, run=run)
    assert not res["ok"] and not any(c[:3] == ["gh", "issue", "close"] for c in calls), calls
    print("ok  apply_plan: a failed mega create closes no child")


def test_existing_mega_absorbs_new_twin() -> None:
    mega = {"number": 9001, "title": "[mega x4] prod alert [pg_lock:1549162:pg_toast_16627]",
            "labels": L("fleet:backlog", ic.MEGA_LABEL, "lane:devops"),
            "body": ic.mega_body({"signature": ic.signature("prod alert [pg_lock:1:pg_toast_16627]"),
                                  "lane": "lane:devops", "children": FIXTURE[:4], "linked": []})}
    fresh = {"number": 7685, "title": "prod alert [pg_lock:3973044:pg_toast_16627]", "body": "",
             "labels": L("fleet:backlog", "lane:devops")}
    plans = ic.plan_megas([mega, fresh], min_size=3)
    assert len(plans) == 1 and plans[0]["mega"] == 9001 and [c["number"] for c in plans[0]["children"]] == [7685]
    body = ic.extend_mega_body(mega["body"], plans[0])
    assert body.count("#7236") == 1 and "- [ ] #7685" in body
    print("ok  plan_megas: an existing mega absorbs a new twin (min_size does not apply)")


def _fake_gh(open_issues):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(open_issues), stderr="")
        if cmd[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/issues/9100\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run, calls


def test_dedupe_helper_comments_on_twin_instead_of_filing() -> None:
    run, calls = _fake_gh(FIXTURE)
    res = ic.file_issue("prod alert [pg_lock:3973044:pg_toast_16627]", "fired again",
                        ["fleet:backlog", "lane:devops"], run=run, who="sentry")
    assert res["action"] == "commented" and res["number"] == 7236, res
    assert not any(c[:3] == ["gh", "issue", "create"] for c in calls), "filed a twin"
    print("ok  file_issue: a twin becomes a comment on the open issue, never a new one")


def test_dedupe_helper_prefers_mega_and_respects_lane() -> None:
    mega = {"number": 9001, "title": "[mega x4] whatever", "labels": L(ic.MEGA_LABEL, "lane:devops"),
            "body": f"{ic.SIG_MARKER} {ic.signature('prod alert [pg_lock:1:pg_toast_16627]')}\n{ic.LANE_MARKER} lane:devops"}
    run, _ = _fake_gh(FIXTURE + [mega])
    res = ic.file_issue("prod alert [pg_lock:1:pg_toast_16627]", "", ["lane:devops"], run=run)
    assert res["number"] == 9001, f"expected the mega, got {res}"
    run, calls = _fake_gh(FIXTURE)
    res = ic.file_issue("prod alert [pg_lock:1:pg_toast_16627]", "", ["lane:ui"], run=run)
    assert res["action"] == "created", f"different lane must file fresh, got {res}"
    print("ok  file_issue: a mega wins over a plain twin; a different lane files fresh")


def test_dedupe_helper_always_files_reif_asks_and_new_causes() -> None:
    run, calls = _fake_gh(FIXTURE)
    res = ic.file_issue("prod alert [pg_lock:9:pg_toast_16627]", "", ["fleet:reif-asked"], run=run)
    assert res["action"] == "created", res
    res = ic.file_issue("prod alert [pg_lock:9:idx_orgs_ein]", "", ["lane:devops"], run=run)
    assert res["action"] == "created", res
    print("ok  file_issue: a Reif ask and a new root cause are always filed")


def test_filer_members_cannot_bypass_the_helper() -> None:
    root = Path(__file__).resolve().parent.parent
    for name in ic.FILER_MEMBERS:
        spec = json.loads((root / "members" / name / f"{name}.fleet.json").read_text())
        deny = ((spec.get("llm") or {}).get("tools") or {}).get("deny") or []
        assert "Bash(gh issue create:*)" in deny, f"{name} can still run raw gh issue create"
    print(f"ok  filer members deny raw gh issue create: {', '.join(ic.FILER_MEMBERS)}")


if __name__ == "__main__":
    fails = 0
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if fails else 0)
