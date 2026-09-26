#!/usr/bin/env python3
"""A minion pass the timeout kills must leave a pushed branch + draft PR, and the next pass on
those items must resume it (2026-09-26: #7938 #7939 #7941 #7950 ran 5400s twice, rc=124, left
nothing, restarted from zero).

Real git (a bare "origin" in a temp dir), a fake `gh` (FLEET_GH_BIN) that records its calls.
RED without the fix: minion_checkpoint.py does not exist; run_member.sh accepts any batch size.
Plain python, no pytest.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import minion_checkpoint as mc  # noqa: E402
from pretest_push_hook import receipt_path  # noqa: E402

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_GH_STATE"]
calls = json.load(open(state)) if os.path.exists(state) else []
calls.append(sys.argv[1:])
json.dump(calls, open(state, "w"))
a = sys.argv[1:]
if a[:2] == ["pr", "list"]:
    made = [c for c in calls if c[:2] == ["pr", "create"]]
    print("4242" if made else "")
elif a[:2] == ["pr", "create"]:
    print("https://github.com/o/r/pull/4242")
'''


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Sandbox:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mc-test-"))
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh(self.tmp, "git", "clone", "-q", str(self.origin), str(self.repo))
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            sh(self.repo, "git", "config", k, v)
        (self.repo / "a.py").write_text("x = 1\n")
        sh(self.repo, "git", "add", "a.py")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "git", "fetch", "-q", "origin")
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.state = self.tmp / "gh.json"
        os.environ["FLEET_GH_BIN"] = str(self.gh)
        os.environ["FAKE_GH_STATE"] = str(self.state)

    def worktree(self, branch: str) -> str:
        wt = self.tmp / branch.replace("/", "_")
        sh(self.repo, "git", "worktree", "add", "-q", "-b", branch, str(wt), "origin/main")
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            sh(wt, "git", "config", k, v)
        return str(wt)

    def calls(self):
        return json.loads(self.state.read_text()) if self.state.exists() else []

    def remote_has(self, branch: str) -> bool:
        return bool(sh(self.repo, "git", "ls-remote", "--heads", "origin", branch))


def test_pick_resume_branch_only_takes_a_subset_newest_first() -> None:
    bs = ["member/minion-item7938-11-100", "member/minion-item7938-12-300",
          "member/minion-item7938_7939_7941_7950-9357-400",   # carries items not handed now
          "member/minion-item7939-13-500", "member/the-fixer-item7938-1-900", "main"]
    assert mc.pick_resume_branch(bs, [7938]) == "member/minion-item7938-12-300"
    assert mc.pick_resume_branch(bs, [7938, 7939, 7941, 7950]) == \
        "member/minion-item7939-13-500"  # newest subset, never a branch with a foreign item
    assert mc.pick_resume_branch(bs, [1234]) is None
    print("ok  resume picks the newest minion branch whose items are all this pass's items")


def test_timed_out_save_commits_wip_pushes_and_opens_one_draft_pr() -> None:
    sb = Sandbox()
    br = "member/minion-item7938-9357-1790390243"
    wt = sb.worktree(br)
    (Path(wt) / "a.py").write_text("x = 2\n")            # tracked edit, uncommitted
    (Path(wt) / "new.py").write_text("y = 1\n")          # small untracked file
    (Path(wt) / "shot.png").write_bytes(b"0" * (mc.WIP_MAX_BYTES + 1))  # too big: left out
    r = mc.save(wt, br, [7938], "timed-out")
    assert r["saved"] and r["wip_commit"] and r["pr"] == 4242, r
    assert sb.remote_has(br), "branch was not pushed"
    files = sh(wt, "git", "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["a.py", "new.py"], files
    create = [c for c in sb.calls() if c[:2] == ["pr", "create"]]
    assert len(create) == 1 and "--draft" in create[0], create
    body = create[0][create[0].index("--body") + 1]
    assert mc.MARKER in body and "Part of #7938" in body and "Closes" not in body, body
    # A second save (next checkpoint) pushes again but never opens a second PR.
    (Path(wt) / "a.py").write_text("x = 3\n")
    r2 = mc.save(wt, br, [7938], "timed-out")
    assert r2["saved"] and r2["pr"] == 4242, r2
    assert len([c for c in sb.calls() if c[:2] == ["pr", "create"]]) == 1
    print("ok  timed-out: WIP committed (small files only), branch pushed, ONE draft PR")


def test_green_save_waits_for_a_passing_receipt_on_head() -> None:
    sb = Sandbox()
    br = "member/minion-item7939-1-2"
    wt = sb.worktree(br)
    assert mc.save(wt, br, [7939], "green")["why"] == "no commits ahead of origin/main"
    (Path(wt) / "a.py").write_text("x = 5\n")
    sh(wt, "git", "commit", "-qam", "build")
    r = mc.save(wt, br, [7939], "green")
    assert not r["saved"] and "receipt" in r["why"] and not sb.remote_has(br), r
    tree = sh(wt, "git", "rev-parse", "HEAD^{tree}")
    receipt_path(wt).write_text(json.dumps({"status": "fail", "content": tree}))
    assert not mc.save(wt, br, [7939], "green")["saved"]
    receipt_path(wt).write_text(json.dumps({"status": "pass", "content": tree}))
    r = mc.save(wt, br, [7939], "green")
    assert r["saved"] and r["pr"] == 4242 and sb.remote_has(br), r
    print("ok  green: saves only once HEAD's exact tree has a passing verified_test receipt")


def test_pre_timeout_save_never_commits_under_a_live_model() -> None:
    sb = Sandbox()
    br = "member/minion-item7941-1-2"
    wt = sb.worktree(br)
    (Path(wt) / "a.py").write_text("x = 7\n")
    sh(wt, "git", "commit", "-qam", "part one")
    (Path(wt) / "a.py").write_text("x = 8\n")            # the model is mid-edit
    r = mc.save(wt, br, [7941], "pre-timeout")
    assert r["saved"] and "wip_commit" not in r, r
    assert sh(wt, "git", "status", "--porcelain") == "M a.py", "pre-timeout touched the index"
    print("ok  pre-timeout: pushes existing commits, leaves the live worktree untouched")


def test_find_reads_the_real_remote_and_run_member_resumes_it() -> None:
    sb = Sandbox()
    br = "member/minion-item7950-5-6"
    wt = sb.worktree(br)
    (Path(wt) / "a.py").write_text("x = 9\n")
    mc.save(wt, br, [7950], "timed-out")
    assert mc.find(str(sb.repo), [7950]) == br
    assert mc.find(str(sb.repo), [7938]) is None
    rm = (HERE / "run_member.sh").read_text()
    assert "minion_checkpoint.py\" find --items" in rm and 'worktree add -B "$RESUME_BRANCH"' in rm
    assert "--reason timed-out" in rm and "minion_checkpoint.py\" watch" in rm
    print("ok  find returns the pushed checkpoint; run_member.sh resumes, watches, saves on 124")


def test_killed_pass_saves_like_a_timeout_and_the_trap_calls_it() -> None:
    # 2026-09-26 06:42: a deploy's retire drain SIGTERMed #7938 #7939 #7941 #7950 34 min in.
    sb = Sandbox()
    br = "member/minion-item7938-36096-1790402919"
    wt = sb.worktree(br)
    (Path(wt) / "a.py").write_text("x = 42\n")           # never committed by the model
    r = mc.save(wt, br, [7938], "killed")
    assert r["saved"] and r["wip_commit"] and sb.remote_has(br), r
    assert mc.find(str(sb.repo), [7938]) == br
    rm = (HERE / "run_member.sh").read_text()
    trap = rm[rm.index("record_killed_pass() {"):rm.index("trap record_killed_pass TERM INT")]
    assert "--reason killed" in trap and "timeout" in trap, "SIGTERM trap does not checkpoint"
    print("ok  killed: the SIGTERM trap WIP-commits and pushes, the next pass can resume it")


def test_run_member_refuses_a_batch_over_target_items() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rm-guard-"))
    env = {**os.environ, "FLEET_ENV_FILE": "/dev/null", "FLEET_REPO": str(tmp),
           "FLEET_LOG_DIR": str(tmp), "FLEET_MINION_TARGET_ITEMS": "3"}
    p = subprocess.run(["bash", str(HERE / "run_member.sh"), "minion", "--items",
                        "7938,7939,7941,7950"], cwd=tmp, env=env, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 2 and "FLEET_MINION_TARGET_ITEMS=3" in p.stderr, (p.returncode, p.stderr[-500:])
    print("ok  run_member.sh refuses a 4-item minion when FLEET_MINION_TARGET_ITEMS=3")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
