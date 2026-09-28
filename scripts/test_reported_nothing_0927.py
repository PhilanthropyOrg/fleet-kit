#!/usr/bin/env python3
"""2026-09-27: 60 of 141 minion runs landed `reported_nothing` ($154 of $299). Per-run session
transcripts put every one of them in two shapes:

  56  ended the turn on a still-running background task ("I'll wait for the verified_test.sh
      completion notification"): in `claude -p` the session ends with the turn, nothing is
      pushed, no report.
   4  wrote the report, left a Monitor/sleep running; its notification fired after the report
      and the one-line reply to it became `result` (num_turns 1-2, cost $1.3-$7.3).

Fixes pinned here: pr_done_hook.py refuses the stop while a launched task has no completion on
record; ScheduleWakeup is denied to minions; a minion that still ends rc=0 with no Outcome: is
checkpointed (WIP commit, push, draft PR) instead of losing the build. Plain python, no pytest.
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
import pr_done_hook  # noqa: E402

# Verbatim tool_result wording from live minion transcripts (Claude Code 2.1.283).
BG = ("Command running in background with ID: {id}. Output is being written to: "
      "/tmp/claude-0/x/tasks/{id}.output. You will be notified when it completes.")
AUTO_BG = ("Command did not complete within its 120s timeout and was moved to the background "
           "(ID: {id}). Output is being written to: /tmp/x/tasks/{id}.output.")
MON = "Monitor started (task {id}, expires in 10m unless the source ends first)."
OUT_DONE = ("<retrieval_status>success</retrieval_status>\n\n<task_id>{id}</task_id>\n\n"
            "<task_type>local_bash</task_type>\n\n<status>completed</status>\n\n<exit_code>0</exit_code>")
OUT_RUNNING = ("<retrieval_status>timeout</retrieval_status>\n\n<task_id>{id}</task_id>\n\n"
               "<task_type>local_bash</task_type>\n\n<status>running</status>")
STOPPED = '{{"message":"Successfully stopped task: {id} (sleep 600)","task_id":"{id}"}}'


def _result(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": text}]}}


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _transcript(*events) -> str:
    p = Path(tempfile.mkdtemp(), "session.jsonl")
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return str(p)


def _env() -> dict:
    # No WT_PATH: isolates the new check from the PR lookup that follows it.
    return {"FLEET_RUN_ID": f"minion-item8216-{os.getpid()}-{os.urandom(4).hex()}",
            "TMPDIR": tempfile.mkdtemp()}


def test_stop_refused_while_a_background_test_is_still_running() -> None:
    # minion-item8216-18127-1790485931: bg verified_test.sh, then "Waiting for the ... to complete."
    t = _transcript(_result(BG.format(id="bxo2kuy06")), _result(AUTO_BG.format(id="b9yrb7nd3")),
                    _say("Waiting for the `verified_test.sh` background run to complete."))
    why = pr_done_hook.decide(_env(), payload={"transcript_path": t})
    assert why and "bxo2kuy06" in why and "b9yrb7nd3" in why and "TaskStop" in why and "FOREGROUND" in why and "TaskOutput(" not in why, why
    print("ok  a pass that ends its turn on a running background task is refused the stop")


def test_stop_refused_while_a_monitor_is_left_running_after_the_report() -> None:
    # minion-item8167_8409_8407-260895: report written, Monitor b6j81gun0 still armed.
    t = _transcript(_result(MON.format(id="b6j81gun0")),
                    _result("PR #8424: GREEN"), _say("Report: ...\nOutcome: #8424 green\nEvidence: `x`"))
    why = pr_done_hook.decide(_env(), payload={"transcript_path": t})
    assert why and "b6j81gun0" in why and "TaskStop" in why, why
    print("ok  a report with a Monitor still armed is refused until it is stopped")


def test_stop_allowed_once_every_task_is_finished_or_stopped() -> None:
    # Mid-turn notifications land as attachment/queued_command, not as a user message
    # (minion-item8231-98956: its Monitor's expiry was only ever recorded this way).
    queued = {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "task-notification",
              "prompt": "<task-notification>\n<task-id>mmm2</task-id>\n<summary>Monitor event</summary>\n"
                        "<event>[Monitor expired after 10m with no events delivered.]</event>\n</task-notification>"}}
    t = _transcript(_result(MON.format(id="mmm2")), queued)
    assert pr_done_hook.pending_tasks(t) == [], pr_done_hook.pending_tasks(t)
    t = _transcript(_result(BG.format(id="aaa1")), _result(MON.format(id="mmm1")), _result(BG.format(id="ccc1")),
                    _result(OUT_RUNNING.format(id="aaa1")),
                    _result(OUT_DONE.format(id="aaa1")), _result(STOPPED.format(id="mmm1")),
                    {"type": "user", "message": {"role": "user", "content":
                        "<task-notification>\n<task-id>ccc1</task-id>\n<status>completed</status>\n</task-notification>"}})
    assert pr_done_hook.pending_tasks(t) == []
    assert pr_done_hook.decide(_env(), payload={"transcript_path": t}) is None
    print("ok  TaskOutput completed / TaskStop / completion notification each clear a task")


def test_model_prose_and_non_fleet_sessions_never_trigger_it() -> None:
    t = _transcript(_say("Monitor started (task zzz9) -- running in background with ID: yyy8"))
    assert pr_done_hook.pending_tasks(t) == []
    t2 = _transcript(_result(BG.format(id="bbb2")))
    assert pr_done_hook.decide({"TMPDIR": tempfile.mkdtemp()}, payload={"transcript_path": t2}) is None
    assert pr_done_hook.decide(_env(), payload={"transcript_path": "/nonexistent/x.jsonl"}) is None
    print("ok  only harness tool results count; inert outside a fleet pass; unreadable = open")


def test_bounded() -> None:
    env, t = _env(), _transcript(_result(BG.format(id="bbb3")))
    res = [pr_done_hook.decide(env, payload={"transcript_path": t}) for _ in range(pr_done_hook.max_blocks(env) + 1)]
    assert res[0] and res[-1] is None, res
    print("ok  lets go after FLEET_PR_DONE_MAX_BLOCKS refusals")


def test_hook_process_reads_the_transcript_from_stdin() -> None:
    t = _transcript(_result(BG.format(id="bbb4")))
    p = subprocess.run([sys.executable, str(HERE / "pr_done_hook.py")],
                       input=json.dumps({"hook_event_name": "Stop", "transcript_path": t}),
                       env={**os.environ, **_env()}, capture_output=True, text=True)
    assert p.returncode == 2 and "bbb4" in p.stderr, (p.returncode, p.stderr)
    print("ok  the real Stop hook process exits 2 with the task id")


def test_minion_cannot_schedule_a_wakeup_it_will_never_get() -> None:
    spec = json.loads((HERE.parent / "members" / "minion" / "minion.fleet.json").read_text())
    assert "ScheduleWakeup" in spec["llm"]["tools"]["deny"]
    print("ok  minion spec denies ScheduleWakeup")


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
calls = json.load(open(log)) if os.path.exists(log) else []
calls.append(a)
json.dump(calls, open(log, "w"))
if a[:2] == ["pr", "list"] and "isDraft" in a:
    print(os.environ.get("FAKE_GH_ISDRAFT", ""))
elif a[:2] == ["pr", "list"]:
    print("4242" if any(c[:2] == ["pr", "create"] for c in calls) else "")
elif a[:2] == ["pr", "create"]:
    print("https://github.com/o/r/pull/4242")
'''


def _sandbox():
    tmp = Path(tempfile.mkdtemp(prefix="rn0927-"))
    sh = lambda cwd, *a: subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    origin, repo = tmp / "o.git", tmp / "repo"
    sh(tmp, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp, "git", "clone", "-q", str(origin), str(repo))
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        sh(repo, "git", "config", k, v)
    (repo / "a.py").write_text("x = 1\n")
    sh(repo, "git", "add", "a.py")
    sh(repo, "git", "commit", "-qm", "init")
    sh(repo, "git", "push", "-q", "origin", "HEAD:main")
    br = "member/minion-item8216-18127-1790485931"
    wt = tmp / "wt"
    sh(repo, "git", "fetch", "-q", "origin")
    sh(repo, "git", "worktree", "add", "-q", "-b", br, str(wt), "origin/main")
    gh = tmp / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    os.environ.update(FLEET_GH_BIN=str(gh), FAKE_GH_LOG=str(tmp / "gh.json"))
    return sh, repo, wt, br


def test_ended_without_a_report_is_checkpointed_not_lost() -> None:
    sh, repo, wt, br = _sandbox()
    (wt / "a.py").write_text("x = 2\n")   # the build, never committed: the model ended "waiting"
    os.environ["FAKE_GH_ISDRAFT"] = ""
    r = mc.save(str(wt), br, [8216], "ended")
    assert r["saved"] and r.get("wip_commit") and r["pr"] == 4242, r
    assert sh(repo, "git", "ls-remote", "--heads", "origin", br), "branch not pushed"
    print("ok  ended rc=0 with no report: WIP committed, pushed, draft PR")


def test_ended_never_pushes_wip_onto_a_ready_pr() -> None:
    sh, repo, wt, br = _sandbox()
    (wt / "a.py").write_text("x = 3\n")
    os.environ["FAKE_GH_ISDRAFT"] = "false"
    r = mc.save(str(wt), br, [8216], "ended")
    assert not r["saved"] and "non-draft" in r["why"], r
    assert not sh(repo, "git", "ls-remote", "--heads", "origin", br)
    os.environ["FAKE_GH_ISDRAFT"] = ""
    print("ok  ended: an open non-draft PR (auto-merge may be armed) never gets untested WIP")


def test_run_member_checkpoints_an_unreported_rc0_minion() -> None:
    rm = (HERE / "run_member.sh").read_text()
    blk = rm[rm.index("--reason ended") - 900:rm.index("--reason ended")]
    assert '"$RC" -eq 0' in blk and "Outcome" in blk and '"$MEMBER" = "minion"' in blk, blk
    assert rm.index("--reason ended") < rm.index("CHECKPOINT_PR=$("), "save must feed CHECKPOINT_PR"
    print("ok  run_member.sh saves an rc=0 minion that wrote no Outcome:, before the record")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 -- report every failure, not just the first
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {str(exc)[:160]}")
    print("all ok" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
