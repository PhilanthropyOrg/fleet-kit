#!/usr/bin/env python3
"""daily_walk -- the morning user walk on prod (philanthropy#13448).

Every morning at 6:00 AM Central (dino host cron, ~/G/bin/daily_walk_cron.sh) the QA personas
walk the three money journeys -- claim -> HQ -> verify, job seeker -> apply, donor -> give --
and one random sample of pages (a 990 report, an org page, a job, a person page, an HQ screen,
an automatic mail). Read-only: nothing is claimed, posted, paid or signed up for.

The journeys live in members/sentry/journeys.yaml (ids walk-*, `walked_by: daily-walk`, so
sentry's 3-hourly pass skips them) and run on journey_walker's own machinery
(scripts/journey_money.py). This script turns the result into:

  ~/G/walk/<YYYY-MM-DD>.md   line 1: "Walk this morning (...): N of M steps pass, F fail, B blocked"
                             then one line per step:
                             PASS|FAIL|BLOCKED · <journey/step> · <url> · "<the words seen>"
  ~/G/walk/<YYYY-MM-DD>.json the same rows, for the 7:00 AM mail and the next re-test

BLOCKED is the checker's problem (a challenge page, a missing credential, a step the walk does
not take on purpose, a step not reached after an earlier fail) and is never written as FAIL.

With --file, every FAIL becomes a `Cruft:` issue in the product repo (at most 5 new a day; the
same journey+step already open gets a "Still broken" comment instead), and every open walk
issue whose step passed today gets a "Passes now" comment. Walk issues carry a `Walk: daily`
line and a `Walk-key: <journey>/<step>` line; ~/G/walk/filed.json remembers keys, so a twin the
fleet's filing door folded us into is followed too. Without --file (a hand run) the plan is
written to the walk file and nothing is filed.

Secrets (ATLAS_TEST_BYPASS, QA_SESSION_TOKEN, sign-in links) are cut from every line written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

CT = zoneinfo.ZoneInfo("America/Chicago")
WALK_IDS = ("walk-claim-hq-verify", "walk-job-seeker-apply", "walk-donor-give", "walk-random-pages")
MARKER = "Walk: daily"
KEY_LINE = "Walk-key:"
MAX_NEW_PER_DAY = 5
PRODUCT_REPO = os.environ.get("PRODUCT_REPO_SLUG", "PhilanthropyOrg/philanthropy")
VP_TAG = {"desktop": "", "mobile_390": "@390", "desktop_1440": "@1440"}
LABELS = ["fleet:backlog", "quality:solid"]


def scrub(text, secrets=()) -> str:
    """One line, no secret: sign-in tokens cut from URLs, known secret values replaced."""
    from journey_money import scrub_url
    t = scrub_url(re.sub(r"\s+", " ", str(text or "")).strip())
    for s in secrets:
        if s:
            t = t.replace(s, "[REDACTED]")
    return t


def rows_from(catalog: dict, journeys_out: list[dict], blocked: list[dict], secrets=(), ids=WALK_IDS) -> list[dict]:
    """One row per catalog step per viewport of every walk journey, in catalog order. A step
    with no result (its journey was blocked, or an earlier step failed) is BLOCKED, never
    FAIL; a walker crash is the checker's fault, so it is BLOCKED too."""
    results = {}
    for out in journeys_out:
        jid, _, vp = out["id"].partition("--")
        results[(jid, vp or "desktop")] = out["steps"]
    why_blocked = {(b["id"], b.get("viewport") or "desktop"): b["reason"] for b in blocked}
    rows = []
    for j in catalog["journeys"]:
        if j["id"] not in ids:
            continue
        for vp in j.get("viewports") or ["desktop"]:
            steps = results.get((j["id"], vp), [])
            crash = next((s for s in steps if str(s.get("action", "")).startswith("(journey_walker internal error)")), None)
            by_index = {s["index"]: s for s in steps if s is not crash}
            first_fail = None
            for i, step in enumerate(j["steps"]):
                r = by_index.get(i)
                if r is None:
                    status = "BLOCKED"
                    if crash is not None:
                        why = "walker error: " + (crash.get("detail") or "").strip().splitlines()[-1][:160]
                    elif (j["id"], vp) in why_blocked:
                        why = why_blocked[(j["id"], vp)]
                    else:
                        why = f"not reached: step {first_fail} failed" if first_fail is not None else "not reached"
                    r = {}
                else:
                    status = {"pass": "PASS", "fail": "FAIL"}.get(r["status"], "BLOCKED")
                    why = r.get("detail") or ""
                    if status == "FAIL" and first_fail is None:
                        first_fail = i
                rows.append({
                    "status": status, "journey": j["id"], "viewport": vp, "index": i,
                    "key": f"{j['id']}/{i}", "label": step.get("label") or step["action"][:40],
                    "url": scrub(r.get("url") or "-", secrets), "seen": scrub(r.get("seen") or "", secrets)[:220],
                    "why": scrub(why, secrets)[:240], "action": step["action"].strip(),
                    "expected": " ".join(step["observable_result"].split()),
                    "owner": str(step.get("owner") or j.get("owner") or ""),
                    "lane_label": step.get("lane_label") or j.get("lane_label") or "bug",
                    "vision_link": j.get("vision_link") or "none (maintenance)",
                    "journey_name": j["name"], "screenshot": r.get("screenshot"),
                })
    return rows


def line(r: dict) -> str:
    out = f'{r["status"]} · {r["journey"]}{VP_TAG.get(r["viewport"], "@" + r["viewport"])}/{r["index"]} {r["label"]} · {r["url"]} · "{r["seen"]}"'
    if r["status"] != "PASS" and r["why"]:
        out += f" · why: {r['why']}"
    return out


def counts(rows: list[dict]) -> tuple[int, int, int, int]:
    p = sum(r["status"] == "PASS" for r in rows)
    f = sum(r["status"] == "FAIL" for r in rows)
    b = sum(r["status"] == "BLOCKED" for r in rows)
    return p, len(rows), f, b


def summary_line(rows: list[dict], when: dt.datetime) -> str:
    p, m, f, b = counts(rows)
    return f"Walk this morning ({when:%Y-%m-%d}, {when:%-I:%M %p} Central): {p} of {m} steps pass, {f} fail, {b} blocked"


def render(rows: list[dict], when: dt.datetime, base_url: str, extra: list[str] | None = None) -> str:
    md = [summary_line(rows, when),
          f"Prod {base_url} · seed {when:%Y-%m-%d} · signed out, then QA owner and operator · read-only · philanthropy#13448",
          ""]
    md += [line(r) for r in rows]
    if extra:
        md += [""] + extra
    return "\n".join(md) + "\n"


# --- filing --------------------------------------------------------------------------------------

def walk_keys(issues: list[dict]) -> dict[str, int]:
    keys = {}
    for iss in issues:
        for m in re.finditer(rf"^{KEY_LINE}\s*(\S+)", iss.get("body") or "", re.M):
            keys.setdefault(m.group(1), iss["number"])
    return keys


def issue_title(fails: list[dict]) -> str:
    r = fails[0]
    journey = r["journey_name"].removeprefix("Daily walk: ")
    return f'Cruft: {journey}: "{r["label"]}" fails for a person on the daily walk'


def issue_body(fails: list[dict], when: dt.datetime) -> str:
    r = fails[0]
    widths = ", ".join(sorted({x["viewport"] for x in fails}))
    gwt_url = r["url"] if r["url"] != "-" else "philanthropy.org"
    return "\n".join([
        f"Vision-link: {r['vision_link']}",
        MARKER,
        f"{KEY_LINE} {r['key']}",
        f"Owner: {r['owner']}",
        "",
        f"The daily user walk (philanthropy#13448) failed this step on {when:%Y-%m-%d} at {when:%-I:%M %p} Central, read-only, at {widths}.",
        "",
        f"- Step: {r['action']}",
        f"- Expected: {r['expected']}",
        f'- Saw: "{r["seen"]}"',
        f"- Why: {r['why']}",
        f"- URL: {r['url']}",
        "",
        f"Given a person on {gwt_url}, When they {r['action'][0].lower() + r['action'][1:].rstrip('.')}, Then {r['expected'][0].lower() + r['expected'][1:].rstrip('.')}.",
        "",
        "Best in class: Checkly and Datadog Synthetics walk each journey as a scripted user on every run and alert on the first failed step.",
        "Strategy: trust (a money journey a real person takes; the walk re-tests this step every morning and comments here)",
    ])


def plan_filing(rows: list[dict], keys: dict[str, int], state_today: dict, when: dt.datetime) -> list[tuple]:
    """What to do with today's rows: ("create", key, fails), ("comment", number, key, text),
    ("over_cap", key, fails). `keys` maps an open walk issue's key to its number; `state_today`
    is today's entry of filed.json (created/commented so far), so a second run the same day
    never re-files or re-comments."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["key"], []).append(r)
    created = len(state_today.get("created", []))
    commented = set(state_today.get("commented", []))
    actions = []
    for key, rs in groups.items():
        fails = [r for r in rs if r["status"] == "FAIL"]
        num = keys.get(key)
        if fails:
            if num:
                if key not in commented:
                    actions.append(("comment", num, key, f"Still broken on {when:%Y-%m-%d} (daily walk, {when:%-I:%M %p} Central):\n\n" + "\n".join(f"    {line(r)}" for r in fails)))
            elif key in state_today.get("created_keys", []):
                continue
            elif created < MAX_NEW_PER_DAY:
                actions.append(("create", key, fails))
                created += 1
            else:
                actions.append(("over_cap", key, fails))
        elif num and key not in commented and all(r["status"] == "PASS" for r in rs):
            actions.append(("comment", num, key, f"Passes now on {when:%Y-%m-%d} (daily walk, {when:%-I:%M %p} Central):\n\n" + "\n".join(f"    {line(r)}" for r in rs)))
    return actions


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "GH_PAGER": "cat"})


def open_walk_issues(repo: str, gh=_gh) -> list[dict]:
    r = gh(["issue", "list", "-R", repo, "--state", "open", "--search", f'"{MARKER}" in:body',
            "--limit", "200", "--json", "number,title,body"])
    if r.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {(r.stderr or '').strip()[:200]}")
    return json.loads(r.stdout or "[]")


def execute(actions: list[tuple], when: dt.datetime, repo: str, state_today: dict, gh=_gh) -> list[str]:
    """Run the plan. Creates go through issue_cluster.file_issue, the fleet's one filing door,
    which folds a twin into a comment."""
    import issue_cluster
    log, open_all = [], None
    for act in actions:
        if act[0] == "comment":
            _, num, key, text = act
            r = gh(["issue", "comment", str(num), "-R", repo, "--body", text])
            log.append(f"{'commented' if r.returncode == 0 else 'COMMENT FAILED'} #{num} ({key}): {text.splitlines()[0]}")
            if r.returncode == 0:
                state_today.setdefault("commented", []).append(key)
        elif act[0] == "create":
            _, key, fails = act
            if open_all is None:
                try:
                    open_all = issue_cluster.list_open(repo, run=lambda c: gh(c[1:]))
                except Exception:  # noqa: BLE001 -- a failed board read still files (the door's own rule)
                    open_all = []
            labels = LABELS + [fails[0]["lane_label"]]
            res = issue_cluster.file_issue(issue_title(fails), issue_body(fails, when), labels, repo=repo,
                                           run=lambda c: gh(c[1:]), open_issues=open_all, who="daily-walk")
            if res.get("action") == "created" and res.get("ok"):
                num = int(re.search(r"/issues/(\d+)", res.get("url") or "/issues/0").group(1))
                state_today.setdefault("created", []).append(num)
                state_today.setdefault("created_keys", []).append(key)
                state_today.setdefault("keys", {})[key] = num
                log.append(f"filed #{num} ({key}): {issue_title(fails)}")
            elif res.get("action") == "commented":
                state_today.setdefault("keys", {})[key] = res["number"]
                state_today.setdefault("commented", []).append(key)
                log.append(f"folded into twin #{res['number']} ({key})")
            else:
                log.append(f"FILE FAILED ({key}): {res.get('error', '')[:160]}")
        elif act[0] == "over_cap":
            log.append(f"not filed, over the {MAX_NEW_PER_DAY}-a-day cap: {act[1]}")
    return log


def describe(actions: list[tuple]) -> list[str]:
    out = []
    for act in actions:
        if act[0] == "comment":
            out.append(f"would comment #{act[1]} ({act[2]}): {act[3].splitlines()[0]}")
        elif act[0] == "create":
            out.append(f"would file ({act[1]}): {issue_title(act[2])} [{', '.join(LABELS + [act[2][0]['lane_label']])}]")
        else:
            out.append(f"would skip, over cap: {act[1]}")
    return out or ["nothing to file or comment"]


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def prune(shots: Path, days: int = 14) -> None:
    cutoff = time.time() - days * 86400
    for d in shots.glob("*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", type=Path, default=ROOT / "members" / "sentry" / "journeys.yaml")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / "G" / "walk")
    ap.add_argument("--shots-dir", type=Path, default=Path.home() / ".cache" / "fleet-kit" / "daily-walk")
    ap.add_argument("--repo", default=PRODUCT_REPO)
    ap.add_argument("--file", action="store_true", help="file FAILs as Cruft: issues and comment on open walk issues")
    ap.add_argument("--journeys", nargs="*", default=list(WALK_IDS))
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    import journey_walker as jw
    from playwright.sync_api import sync_playwright

    started = dt.datetime.now(CT)
    os.environ.setdefault("DAILY_WALK_SEED", f"{started:%Y-%m-%d}")
    catalog = jw.load_catalog(args.catalog)
    users = jw.TestUsers()
    secrets = (users.bypass, users.qa_session_token)
    run_id = f"{started:%Y-%m-%d}-{int(time.time())}"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], headless=not args.headed)
        try:
            journeys_out, blocked = jw.run_all(catalog, users, browser, args.shots_dir, run_id, args.journeys)
        finally:
            browser.close()

    rows = rows_from(catalog, journeys_out, blocked, secrets, ids=tuple(args.journeys))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path, json_path = args.out_dir / f"{started:%Y-%m-%d}.md", args.out_dir / f"{started:%Y-%m-%d}.json"
    json_path.write_text(json.dumps({"started": started.isoformat(), "rows": rows}, indent=1))

    state_path = args.out_dir / "filed.json"
    state = load_state(state_path)
    today = state.setdefault(f"{started:%Y-%m-%d}", {})
    try:
        issues = open_walk_issues(args.repo)
        open_nums = {i["number"] for i in issues}
        keys = {k: n for day in state.values() for k, n in (day.get("keys") or {}).items() if n in open_nums}
        keys.update(walk_keys(issues))
        actions = plan_filing(rows, keys, today, started)
        if args.file:
            extra = ["Filed:"] + (execute(actions, started, args.repo, today) or ["nothing to file or comment"])
            state_path.write_text(json.dumps(state, indent=1, sort_keys=True))
        else:
            extra = ["Hand run, nothing filed. The plan:"] + describe(actions)
    except Exception as exc:  # noqa: BLE001 -- a failed filing must not cost the walk file
        extra = [f"Filing skipped: {type(exc).__name__}: {scrub(exc, secrets)[:200]}"]
    extra.append(f"Screens: {args.shots_dir}/{run_id}/journeys/<journey>/<viewport>/<step>.png")
    md_path.write_text(render(rows, started, users.base_url, extra))
    prune(args.shots_dir)
    print(summary_line(rows, started))
    print(f"daily_walk: wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
