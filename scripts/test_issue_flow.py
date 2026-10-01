#!/usr/bin/env python3
"""issue_flow + the Filed-by stamp + the journey filer's reopen, driven through a fake `gh` on PATH.

Shapes are the product board's own on 2026-10-01: judge-judy's "fix: PR #N failed code review"
and the-fixer's "PR #N is red" items left open after their PR merged (32 of 34 open that day),
journey issues re-filed after their own "Passing again" close (139 of 199 in 14 days), and
issues nobody could trace to a filer (577 of 2,213).

RED without the change: scripts/issue_flow.py does not exist, file_issue writes no Filed-by
line, and a journey step that fails again files a second issue. Plain python, no pytest.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_GH_STATE"]
st = json.load(open(path))
a = sys.argv[1:]
st.setdefault("calls", []).append(a)
def opt(name, default=None):
    return a[a.index(name) + 1] if name in a else default
def save():
    json.dump(st, open(path, "w"))
out = ""
if a[:2] == ["issue", "list"]:
    out = json.dumps([{"number": int(n), "title": i["title"], "body": i["body"],
                       "labels": [{"name": x} for x in i.get("labels", [])]}
                      for n, i in st["issues"].items() if i["state"] == "OPEN"])
elif a[:2] == ["issue", "create"]:
    n = max([int(k) for k in st["issues"]] + [99]) + 1
    st["issues"][str(n)] = {"title": opt("--title"), "body": opt("--body"), "state": "OPEN",
                            "stateReason": None, "comments": [],
                            "labels": [a[i + 1] for i, x in enumerate(a) if x == "--label"]}
    out = f"https://github.com/o/r/issues/{n}"
elif a[:2] == ["issue", "comment"]:
    st["issues"][a[2]]["comments"].append(opt("--body"))
elif a[:2] == ["issue", "close"]:
    st["issues"][a[2]].update(state="CLOSED", stateReason="COMPLETED")
    st["issues"][a[2]]["comments"].append(opt("--comment"))
elif a[:2] == ["issue", "reopen"]:
    st["issues"][a[2]].update(state="OPEN", stateReason="REOPENED")
    st["issues"][a[2]]["comments"].append(opt("--comment"))
elif a[:2] == ["issue", "view"]:
    i = st["issues"][a[2]]
    out = json.dumps({"state": i["state"], "stateReason": i["stateReason"]})
elif a[:2] == ["pr", "list"]:
    out = json.dumps(st.get("merged" if opt("--state") == "merged" else "closed_prs", []))
elif a[:1] == ["api"]:
    out = "\n".join(json.dumps(r) for r in st.get("api_rows", []))
save()
print(out)
'''


def sandbox(state: dict) -> tuple[dict, Path]:
    d = Path(tempfile.mkdtemp())
    gh = d / "gh"
    gh.write_text(FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    sf = d / "state.json"
    sf.write_text(json.dumps({"issues": {}, **state}))
    env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "FAKE_GH_STATE": str(sf)}
    env.pop("FLEET_MEMBER", None)
    env.pop("FLEET_JOURNEY_REOPEN_DAYS", None)
    return env, sf


def run(script: str, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / script), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def issue(title, body="", labels=(), state="OPEN"):
    return {"title": title, "body": body, "labels": list(labels), "state": state,
            "stateReason": None, "comments": []}


GOOD = ("Evidence.\n\n## Acceptance\n- Given a visitor opens the claim page, when it loads, "
        "then the stage label names the real step.\n\nVision-link: none (maintenance)")


def test_filing_through_the_shared_door_stamps_who_filed() -> None:
    env, sf = sandbox({})
    r = run("issue_cluster.py", "file", "--title", "Search box loses focus", "--body", GOOD,
            "--label", "fleet:backlog", env={**env, "FLEET_MEMBER": "nerd"})
    assert r.returncode == 0, r.stderr
    body = json.loads(sf.read_text())["issues"]["100"]["body"]
    assert body.rstrip().endswith("Filed-by: nerd"), body
    import issue_flow
    assert issue_flow.source_of({"title": "Search box loses focus", "body": body}) == "nerd"
    # no member name known (a person at a shell): the body is left exactly as written
    env2, sf2 = sandbox({})
    run("issue_cluster.py", "file", "--title", "t", "--body", GOOD, "--label", "fleet:backlog", env=env2)
    assert json.loads(sf2.read_text())["issues"]["100"]["body"] == GOOD


def test_source_falls_back_to_the_filers_own_shapes() -> None:
    import issue_flow
    src = issue_flow.source_of
    assert src({"title": "fix: PR #9057 failed code review -- x", "body": ""}) == "judge-judy"
    assert src({"title": "prod alert [pg_lock:1:t]", "body": "Filed by intake"}) == "prod-alert"
    assert src({"title": "Sign in: \"x\" isn't working",
                "body": "<!-- fleet:sentry-journey key=sign-in::step0 -->"}) == "sentry"
    assert src({"title": "growth idea", "body": "", "labels": [{"name": "origin:scout-growth"}]}) == "scout-growth"
    assert src({"title": "HQ feed is slow", "body": "prose"}) == "unstamped"
    assert src({"title": "prod alert [x]", "body": "a\n\nFiled-by: prod-alert"}) == "prod-alert"


BOARD = {
    "1": issue("fix: PR #10 failed code review -- - **high** -- a", labels=["fleet:backlog"]),
    "2": issue("the-fixer: PR #11 is red on a non-mechanical failure", labels=["fleet:reif-priority"]),
    "3": issue("the-fixer: PR #12 is red on a non-mechanical failure", labels=["fleet:reif-priority"]),
    "4": issue("the-fixer: PR #10 is red on a non-mechanical failure", labels=["fleet:reif-priority"]),
    "20": issue("prod alert [site_down]", labels=["lane:devops"]),
    "21": issue("prod alert [site_down]", labels=["lane:devops"]),
    "22": issue("Sign out: \"Click the sign-out control\" isn't working",
                body="<!-- fleet:sentry-journey key=sign-out::step0 -->"),
    "23": issue("Sign out: \"Tap sign out\" isn't working",
                body="<!-- fleet:sentry-journey key=sign-out::step0 -->"),
    "30": issue("Page width is wrong on phones", labels=["fleet:reif-asked"]),
    "31": issue("Page width is wrong on phones"),
    "32": issue("Page width is wrong on phones", labels=["fleet:claimed"]),
    "40": issue("Report never links to similar orgs"),
    "41": issue("Something nobody built yet"),
    "50": issue("Claim funnel counts bots", labels=["lane:ui"]),
    "51": issue("Claim funnel counts bots", labels=["lane:datadog"]),
}
PRS = {"merged": [{"number": 10, "closingIssuesReferences": []},
                  {"number": 77, "closingIssuesReferences": [{"number": 40}]}],
       "closed_prs": [{"number": 12}]}


def test_prune_lists_what_can_close_without_a_build_and_closes_nothing() -> None:
    env, sf = sandbox({"issues": BOARD, **PRS})
    r = run("issue_flow.py", "prune", "--repo", "o/r", env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    # PR #10 merged, #12 closed unmerged, #11 still open: its item stays off the list
    assert [(x["number"], x["pr"]) for x in out["pr_done"]] == [(1, 10), (3, 12), (4, 10)], out["pr_done"]
    assert "PR #10 merged" in out["pr_done"][0]["reason"] and "PR #12 closed" in out["pr_done"][1]["reason"]
    twins = {t["keep"]: t["close"] for t in out["twins"]}
    # same title; same journey marker under two titles; a Reif ask keeps, a claimed twin is left alone
    assert twins == {20: [21], 22: [23], 30: [31]}, twins
    assert [(x["number"], x["prs"]) for x in out["shipped"]] == [(40, [77])], out["shipped"]
    assert out["counts"] == {"pr_done": 3, "twins": 3, "shipped": 1} and out["open"] == len(BOARD)
    calls = json.loads(sf.read_text())["calls"]
    assert len(calls) == 3 and all(c[1] == "list" for c in calls), f"reads only, three of them: {calls}"


def test_flow_counts_opened_and_closed_per_filer() -> None:
    import datetime as dt
    import issue_flow
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda h: (now - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    rows = [
        {"number": 1, "title": "prod alert [a]", "body": "", "labels": [], "created_at": iso(2),
         "closed_at": iso(1), "state_reason": "completed"},
        {"number": 2, "title": "prod alert [b]", "body": "", "labels": [], "created_at": iso(3),
         "closed_at": None, "state_reason": None},
        {"number": 3, "title": "fix: PR #9 failed code review", "body": "", "labels": [],
         "created_at": iso(90), "closed_at": iso(4), "state_reason": "not_planned"},
        {"number": 4, "title": "Feed is slow", "body": "x\n\nFiled-by: nerd", "labels": [],
         "created_at": iso(5), "closed_at": None, "state_reason": None},
        {"number": 5, "title": "old and untouched in the window", "body": "", "labels": [],
         "created_at": iso(200), "closed_at": iso(100), "state_reason": "completed"},
    ]
    env, _ = sandbox({"api_rows": rows})
    r = run("issue_flow.py", "flow", "--repo", "o/r", "--hours", "24", env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    by = {s["source"]: s for s in out["sources"]}
    assert (out["opened"], out["closed"], out["not_built"], out["net"]) == (3, 2, 1, 1), out
    assert by["prod-alert"] == {"source": "prod-alert", "opened": 2, "closed": 1, "not_built": 0, "net": 1}
    assert by["judge-judy"]["closed"] == 1 and by["judge-judy"]["not_built"] == 1 and by["nerd"]["opened"] == 1
    line = run("issue_flow.py", "flow", "--repo", "o/r", "--line", env=env).stdout.strip()
    assert line.startswith("Board, last 24h: 3 opened, 2 closed (1 without a build), net +1. By filer: prod-alert +2/-1"), line
    # the morning brief carries the same line, and still goes out when the read fails
    import messenger_brief
    os.environ.pop("FLEET_REPO_URL", None)
    assert messenger_brief.issue_flow_since(24) == {}, "no product repo configured: no read at all"
    os.environ.update(PATH=env["PATH"], FAKE_GH_STATE=env["FAKE_GH_STATE"],
                      FLEET_REPO_URL="https://github.com/o/r.git")
    assert messenger_brief.issue_flow_since(24)["line"] == line
    os.environ["FAKE_GH_STATE"] = "/nonexistent/state.json"
    assert messenger_brief.issue_flow_since(24) == {}


def _journey(status: str, run_id: str) -> dict:
    return {"run": run_id, "deploy_sha": "sha", "journeys": [{"id": "sign-out", "name": "Sign out", "steps": [
        {"index": 0, "action": "Click the sign-out control", "observable_result": "Signed out", "status": status}]}]}


def _walk(env: dict, tmp: Path, status: str, run_id: str) -> dict:
    rf = tmp / f"{run_id}.json"
    rf.write_text(json.dumps(_journey(status, run_id)))
    code = ("import json,sys,pathlib,journey_issue_filer as j;"
            "print(json.dumps(j.process(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), repo='o/r')))")
    r = subprocess.run([sys.executable, "-c", code, str(rf), str(tmp / "jstate.json")], env=env,
                       cwd=str(HERE), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_flapping_journey_step_reopens_its_issue_when_turned_on() -> None:
    env, sf = sandbox({})
    env["FLEET_JOURNEY_REOPEN_DAYS"] = "14"
    tmp = sf.parent
    assert len(_walk(env, tmp, "fail", "r1")["filed"]) == 1
    assert _walk(env, tmp, "pass", "r2")["closed"][0]["issue"] == 100
    _walk(env, tmp, "pass", "r3")  # a second clean run must not forget which issue it closed
    s4 = _walk(env, tmp, "fail", "r4")
    issues = json.loads(sf.read_text())["issues"]
    assert list(issues) == ["100"], f"a flap must not file a twin: {list(issues)}"
    assert issues["100"]["state"] == "OPEN" and "Failing again on run `r4`" in issues["100"]["comments"][-1]
    assert s4["filed"] == [] and s4["commented"][0]["issue"] == 100 and s4["commented"][0]["reopened"]
    assert issues["100"]["body"].rstrip().endswith("Filed-by: sentry")
    # a member closed it as not planned: that was a decision, so the next failure files fresh
    _walk(env, tmp, "pass", "r5")
    st = json.loads(sf.read_text())
    st["issues"]["100"]["stateReason"] = "NOT_PLANNED"
    sf.write_text(json.dumps(st))
    assert len(_walk(env, tmp, "fail", "r6")["filed"]) == 1


def test_the_reopen_is_off_unless_asked_for() -> None:
    env, sf = sandbox({})
    tmp = sf.parent
    _walk(env, tmp, "fail", "r1"); _walk(env, tmp, "pass", "r2")
    assert len(_walk(env, tmp, "fail", "r3")["filed"]) == 1
    assert sorted(json.loads(sf.read_text())["issues"]) == ["100", "101"], "default behaviour changed"


def test_the_members_who_own_the_duty_are_told_and_runs_carry_their_name() -> None:
    marie = (ROOT / "members" / "marie" / "marie.md").read_text()
    part_b, part_c = marie.index("## Part B — cruft prune"), marie.index("## Part C0")
    at = marie.index("issue_flow.py prune")
    assert part_b < at < part_c, "the prune list belongs in Part B, before any ranking"
    assert "Pruned:" in marie[part_b:part_c]
    messenger = (ROOT / "members" / "dont-shoot-the-messenger" / "dont-shoot-the-messenger.md").read_text()
    assert "`issue_flow.line`" in messenger
    assert 'export FLEET_MEMBER="$MEMBER"' in (HERE / "run_member.sh").read_text()
    import inbox
    calls = []

    def fake(cmd):
        calls.append(cmd)
        out = "[]" if cmd[:3] == ["gh", "issue", "list"] else "https://github.com/o/r/issues/7"
        return subprocess.CompletedProcess(cmd, 0, out, "")
    os.environ["FLEET_REPO_URL"] = "https://github.com/o/r.git"
    inbox.file_or_comment_alert({"check": "site_down", "text": "down", "from": "box-log"}, run=fake)
    body = calls[-1][calls[-1].index("--body") + 1]
    assert body.endswith("Filed-by: prod-alert"), body


if __name__ == "__main__":
    fails = 0
    for name, fn in [(k, v) for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
            print(f"ok  {name}")
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    raise SystemExit(1 if fails else 0)
