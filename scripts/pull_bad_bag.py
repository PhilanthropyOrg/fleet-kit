#!/usr/bin/env python3
"""pull_bad_bag.py -- when main's deploy test gate goes red, pull the PR that broke it off main.

Reif, 2026-09-29: "think of us like the baggage claim, if a bag is messed up, we pull it because
we need the other bags to still pass. every bag must be delivered, but it can come out of order."

THE GAP (philanthropy #8805, 2026-09-29): #8772 and #8797 each passed PR CI, then main's deploy
gate failed at 15:16Z. Nothing shipped for ~50 min -- including a Postgres connection fix --
while the-fixer and a human session both hand-wrote the same fix-forward. Every other merged PR
waited behind the two bad ones.

What this does, with no model in the loop:
  1. Read the failing test ids from the red deploy run's log. A timeout / runner anomaly is not
     a bad bag: hand off to the-fixer as before.
  2. Re-run just those tests on current main. Already green (someone fixed forward): done.
  3. Bisect the merges since the last green deploy, running only the failing tests, to find the
     merge that broke them. Repeat for any still failing after reverting it (max 3 bags).
  4. Pull: one revert PR per bad merge, auto-merge armed. Proof first: the failing tests pass
     with the reverts applied, or nothing is pushed.
  5. Deliver anyway: one re-land PR per bad merge (the revert, reverted) whose body carries
     `Must pass: <tests it broke>`. philanthropy's PR test lane runs those, so the re-land starts
     RED and the fleet's red-PR fixers own it; green, it auto-merges.

    python3 pull_bad_bag.py [--run-id N] [--dry-run]   # exit 0 pulled/nothing to pull, 1 hand off
    python3 pull_bad_bag.py --owns <sha>               # exit 0 mine, 1 handed off, 2 never seen

State ($FLEET_LOG_DIR/pull_bad_bag.state.json) lets the-fixer's check.sh leave a red deploy to
this script while it works, and take it back the moment this hands off.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR") or "/var/log/fleet-kit")
STATE = LOG_DIR / "pull_bad_bag.state.json"
LOG = LOG_DIR / "pull_bad_bag.log"
DEPLOY_WF = os.environ.get("FIXER_DEPLOY_WORKFLOW", "deploy.yml")
MAIN = os.environ.get("FIXER_DEFAULT_BRANCH", "main")
MAX_BAGS = 3
OWN_FOR_S = 90 * 60  # a "pulling" claim older than this is presumed dead: the-fixer takes it back

# deploy_test_gate.sh / deploy.yml markers for a red gate that is NOT a code regression.
INFRA_MARKERS = ("TEST_GATE_TIMEOUT", "WORKSPACE_MISMATCH")
_TEST_ID = re.compile(r"\b(?:FAILED|ERROR) (tests/\S+?\.py(?:::[^\s]+)?)(?=\s|$)")
_PR_NUM = re.compile(r"\(#(\d+)\)\s*$")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------- pure parts (tested in test_pull_bad_bag.py) ----------

def failed_tests(log_text: str) -> list[str] | None:
    """Failing test ids from a pytest log, in first-seen order. None = infra, not a bad bag."""
    if any(m in log_text for m in INFRA_MARKERS):
        return None
    seen: dict[str, None] = {}
    for m in _TEST_ID.finditer(log_text):
        seen[m.group(1).rstrip(".,;")] = None
    return list(seen)


def first_bad(commits: list[str], is_bad) -> int:
    """Index of the first commit where is_bad() holds. The parent of commits[0] is known good and
    commits[-1] is known bad, so the last one is never re-run."""
    lo, hi = 0, len(commits) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if is_bad(commits[mid]):
            hi = mid
        else:
            lo = mid + 1
    return lo


def pr_of(subject: str) -> str | None:
    m = _PR_NUM.search(subject)
    return m.group(1) if m else None


def must_pass_line(tests: list[str]) -> str:
    return "Must pass: " + " ".join(tests)


def pull_body(pr: str, subject: str, broke: list[str], run_url: str) -> str:
    return (
        f"#{pr} broke main's deploy test gate ({run_url}), so it comes off main and every other "
        f"merged PR can ship. It is not dropped: its re-land PR puts it back once these pass.\n\n"
        f"Pulled: {subject}\n\n"
        f"{must_pass_line(broke)}\n\n"
        "Proof: with this revert on current main the tests above pass; before it they fail.\n\n"
        "Opened by fleet-kit `scripts/pull_bad_bag.py` (baggage claim: a bad bag is pulled, "
        "the rest keep moving).\n"
    )


def reland_body(pr: str, pull_pr: str, subject: str, broke: list[str]) -> str:
    return (
        f"Puts #{pr} back after #{pull_pr} pulled it off main. It broke these tests on main; "
        f"CI runs them here, so this PR is red until the change is fixed, then auto-merges.\n\n"
        f"Re-lands: {subject}\n\n"
        f"{must_pass_line(broke)}\n\n"
        "Fixer: change the code (or the test, if the test is what is wrong) until the tests "
        "above pass. Do not drop them from the line.\n"
    )


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def set_state(sha: str, status: str, **extra) -> None:
    st = load_state()
    st[sha] = {"status": status, "ts": int(time.time()), **extra}
    st = dict(sorted(st.items(), key=lambda kv: kv[1].get("ts", 0))[-50:])
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(STATE)
    except OSError as e:
        log(f"WARN: could not write state: {e}")


def owns(sha: str, now: float | None = None) -> int:
    """0 = this script has the red deploy at sha (working on it, or pulled it); 1 = handed off
    (or its claim went stale); 2 = never seen."""
    ent = next((v for k, v in load_state().items() if k.startswith(sha) or sha.startswith(k)), None)
    if not ent:
        return 2
    if ent["status"] == "pulling" and (now or time.time()) - ent.get("ts", 0) > OWN_FOR_S:
        return 1
    return 0 if ent["status"] in ("pulling", "pulled", "clear") else 1


# ---------- side effects ----------

def sh(*args: str, cwd: str | None = None, check: bool = True, timeout: int = 300) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}... rc={r.returncode}: {r.stderr.strip()[-400:]}")
    return r.stdout


def run_tests(wt: str, tests: list[str]) -> set[str]:
    """Which of `tests` fail in worktree wt. A test whose file is absent here cannot fail here."""
    present = [t for t in tests if (Path(wt) / t.split("::")[0]).exists()]
    if not present:
        return set()
    py = sh("bash", str(KIT / "test_python.sh"), wt, cwd=wt).strip().splitlines()[-1]
    # No test slot: these runs are a handful of test files, and a red main blocks every merge
    # behind it -- queueing behind minions' suite runs made the #8805 replay take 11 min.
    runner = 'if command -v eatmydata >/dev/null; then exec eatmydata "$@"; else exec "$@"; fi'
    env = {**os.environ, "PYTHONPATH": f"{wt}/src"}
    r = subprocess.run(
        ["bash", "-c", runner, "run", py, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider",
         *present],
        cwd=wt, capture_output=True, text=True, timeout=1800, env=env,
    )
    return read_result(present, r.returncode, r.stdout + r.stderr)


_PYTEST_SUMMARY = re.compile(r"\b\d+ (?:passed|failed|errors?|skipped|deselected)\b|no tests ran")


def read_result(present: list[str], rc: int, out: str) -> set[str]:
    """Failing subset of `present` from one pytest run. No pytest summary line means pytest never
    ran (missing module, broken interpreter): raise, so the caller hands off instead of reading
    silence as green and standing down on a red main."""
    if not _PYTEST_SUMMARY.search(out):
        raise RuntimeError(f"pytest did not run (rc={rc}): {out.strip()[-300:]}")
    bad = {t for t in present if any(f.startswith(t) or t.startswith(f) for f in failed_tests(out) or [])}
    if rc not in (0, 1) and not bad:
        # pytest itself broke (collection error, rc 2-4): count every requested test as failing.
        bad = set(present)
    return bad


def gh_json(*args: str, cwd: str) -> object:
    return json.loads(sh("gh", *args, cwd=cwd) or "null")


def find_runs(repo: str, run_id: str | None) -> tuple[dict, dict | None]:
    runs = gh_json("run", "list", "--workflow", DEPLOY_WF, "--branch", MAIN, "--limit", "40",
                   "--json", "databaseId,conclusion,headSha,createdAt,url", cwd=repo)
    bad = next((r for r in runs if (str(r["databaseId"]) == run_id if run_id
                                    else r["conclusion"] == "failure")), None)
    if not bad:
        raise RuntimeError("no failed deploy run found")
    good = next((r for r in runs if r["conclusion"] == "success" and r["createdAt"] < bad["createdAt"]),
                None)
    return bad, good


def arm(pr_url: str, cwd: str) -> None:
    r = subprocess.run(["bash", "-c", f'. "{KIT}/merge_arm.sh"; arm_pr_auto_merge "$1"', "arm", pr_url],
                       cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"WARN: auto-merge not armed on {pr_url}: {r.stdout.strip()} {r.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true", help="find the bags, push nothing")
    ap.add_argument("--owns", metavar="SHA")
    ap.add_argument("--claim", metavar="SHA", help="mark sha as being pulled (check.sh, before launch)")
    a = ap.parse_args(argv)
    if a.owns:
        return owns(a.owns)
    if a.claim:
        set_state(a.claim, "pulling")
        return 0

    repo = os.environ.get("FLEET_REPO") or "/repo"
    bad_run, good_run = find_runs(repo, a.run_id)
    sha = bad_run["headSha"]
    done = next((v["status"] for k, v in load_state().items() if sha.startswith(k)), "")
    if done in ("pulled", "clear", "handoff") and not a.dry_run:
        log(f"{sha[:12]}: already {done} -- nothing to do")
        return 0
    if not a.dry_run:
        set_state(sha, "pulling", run=bad_run["url"])

    def hand_off(why: str) -> int:
        log(f"{sha[:12]}: HAND OFF to the-fixer -- {why}")
        if not a.dry_run:
            set_state(sha, "handoff", why=why)
            # Wake the-fixer now rather than on its next tick: its check.sh sees the hand-off.
            rm = KIT / "run_member.sh"
            if rm.exists():
                subprocess.Popen([str(rm), "the-fixer"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        return 1

    try:
        text = sh("gh", "run", "view", str(bad_run["databaseId"]), "--log-failed", cwd=repo, timeout=120)
    except RuntimeError as e:
        return hand_off(f"could not read the run log: {e}")
    tests = failed_tests(text)
    if tests is None:
        return hand_off("infra failure (timeout / checkout anomaly), not a bad bag")
    if not tests:
        return hand_off("no failing test ids in the log (a non-test step failed)")
    if not good_run:
        return hand_off("no green deploy in the last 40 runs to bisect from")
    log(f"{sha[:12]}: deploy gate red on {len(tests)} test(s): {' '.join(tests[:6])}")

    sh("git", "fetch", "-q", "origin", MAIN, cwd=repo, timeout=180)
    tip = sh("git", "rev-parse", f"origin/{MAIN}", cwd=repo).strip()
    wt = tempfile.mkdtemp(prefix="pull-bad-bag-")
    sh("git", "worktree", "add", "-q", "--detach", wt, tip, cwd=repo)
    try:
        return _pull(a, repo, wt, sha, tip, good_run["headSha"], tests, bad_run["url"], hand_off)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return hand_off(f"error: {e}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo, capture_output=True)


def _pull(a, repo, wt, sha, tip, good, tests, run_url, hand_off) -> int:
    def at(commit: str) -> None:
        sh("git", "checkout", "-q", "--detach", commit, cwd=wt)

    still = run_tests(wt, tests)
    if not still:
        log(f"{sha[:12]}: those tests already pass on main {tip[:12]} (fixed forward) -- nothing to pull")
        if not a.dry_run:
            set_state(sha, "clear")
        return 0
    commits = sh("git", "rev-list", "--first-parent", "--reverse", f"{good}..{tip}", cwd=wt).split()
    if not commits:
        return hand_off(f"no merges between last green {good[:12]} and main")
    cache: dict[str, set[str]] = {}
    bags: list[tuple[str, list[str]]] = []  # (commit, tests it broke)
    remaining = set(still)
    while remaining and len(bags) < MAX_BAGS:
        want = sorted(remaining)

        def is_bad(c: str) -> bool:
            if c not in cache:
                at(c)
                cache[c] = run_tests(wt, sorted(still))
            return bool(cache[c] & set(want))

        i = first_bad(commits, is_bad)
        c = commits[i]
        if any(c == b for b, _ in bags):
            break
        broke = sorted(cache.get(c, remaining) & remaining)
        bags.append((c, broke))
        log(f"{sha[:12]}: bad bag {c[:12]} {sh('git', 'log', '-1', '--format=%s', c, cwd=wt).strip()!r} breaks {broke}")
        at(tip)
        for b, _ in bags:
            if not _revert(wt, b):
                return hand_off(f"revert of {b[:12]} does not apply cleanly on main")
        remaining = run_tests(wt, sorted(still))
    if not bags:
        return hand_off("bisect found no bad merge (flaky, or broken by something outside main)")
    if remaining:
        return hand_off(f"still failing after pulling {len(bags)} bag(s): {sorted(remaining)}")
    log(f"{sha[:12]}: PROOF -- with {len(bags)} revert(s) on {tip[:12]} all {len(still)} failing test(s) pass")
    if a.dry_run:
        return 0

    pulled = []
    for c, broke in bags:
        subject = sh("git", "log", "-1", "--format=%s", c, cwd=wt).strip()
        pr = pr_of(subject) or c[:10]
        at(tip)
        _revert(wt, c)
        branch = f"pull/{pr}"
        sh("git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}", cwd=wt, timeout=180)
        pull_url = sh("gh", "pr", "create", "--base", MAIN, "--head", branch,
                      "--title", f"Pull #{pr} off main: it breaks {len(broke)} test(s) on the deploy gate",
                      "--body", pull_body(pr, subject, broke, run_url), cwd=wt).strip()
        arm(pull_url, wt)
        pull_pr = pull_url.rstrip("/").rsplit("/", 1)[-1]
        if not _revert(wt, "HEAD"):
            return hand_off(f"could not build the re-land branch for #{pr} (pull PR {pull_url} is up)")
        rbranch = f"reland/{pr}"
        sh("git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{rbranch}", cwd=wt, timeout=180)
        bare = _PR_NUM.sub("", subject).strip()
        reland_url = sh("gh", "pr", "create", "--base", MAIN, "--head", rbranch,
                        "--title", f"Re-land #{pr}: {bare}"[:250],
                        "--body", reland_body(pr, pull_pr, subject, broke), cwd=wt).strip()
        arm(reland_url, wt)
        if pr_of(subject):
            subprocess.run(["gh", "pr", "comment", pr, "--body",
                            f"This broke main's deploy gate ({', '.join(broke)}). Pulled off main by "
                            f"{pull_url}; it comes back in {reland_url} once those pass."],
                           cwd=wt, capture_output=True, text=True)
        log(f"{sha[:12]}: PULLED #{pr} -> {pull_url} ; re-land -> {reland_url}")
        pulled.append({"pr": pr, "pull": pull_url, "reland": reland_url, "broke": broke})
    set_state(sha, "pulled", bags=pulled)
    return 0


def _revert(wt: str, commit: str) -> bool:
    parents = sh("git", "rev-list", "--parents", "-n1", commit, cwd=wt).split()[1:]
    args = ["git", "-c", "user.name=fleet-kit", "-c", "user.email=fleet-kit@users.noreply.github.com",
            "revert", "--no-edit", *(["-m", "1"] if len(parents) > 1 else []), commit]
    if subprocess.run(args, cwd=wt, capture_output=True, text=True).returncode == 0:
        return True
    subprocess.run(["git", "revert", "--abort"], cwd=wt, capture_output=True)
    return False


if __name__ == "__main__":
    sys.exit(main())
