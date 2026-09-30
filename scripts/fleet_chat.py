#!/usr/bin/env python3
"""fleet_chat.py -- a signed-in person asks Claude about this fleet from the dashboard.

POST /api/chat starts one `claude -p` pass in a background thread; the page polls
/api/chat/poll until it is done (a pass outlives the tunnel's 100s response limit).

What the pass can do, and why it is safe to hand to a second operator:
  --restricted     no shell, no web, user/project settings ignored, file tools confined to
                   the kit, the log dir and the repo checkout.
  deny rules       fleet.env and anything named like a secret is unreadable even in there.
  one Bash command fleet_chat_act.py -- the dashboard's own routes over localhost, with the
                   asker's name on every write (X-Fleet-On-Behalf). So the chat can do what
                   the asker's buttons can do, never more, and writes.jsonl says who.
The keys themselves never reach the pass: FLEET_API_KEY / FLEET_OPERATOR_KEYS are stripped
from its environment, same rule as run_member.sh.

Runs through account_pool_run like every member pass, so a gated account is skipped and
the spend lands on the same pool.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
ENV_FILE = Path(os.environ.get("FLEET_ENV_FILE", KIT_DIR / "fleet.env"))
ACT = KIT_DIR / "scripts" / "fleet_chat_act.py"

MAX_QUESTION = 4000
MAX_HISTORY = 6
TIMEOUT_S = int(os.environ.get("FLEET_CHAT_TIMEOUT_S", "300"))
KEEP_S = 3600          # a finished job is readable for an hour, then forgotten
SECRET_ENV = ("FLEET_API_KEY", "FLEET_OPERATOR_KEYS")
DENY = ["Read(**/fleet.env*)", "Read(**/*.env)", "Read(**/.env*)", "Read(**/*secret*)",
        "Read(**/*token*)", "Read(**/*credential*)", "Read(**/.claude*/**)"]

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def _env() -> dict:
    """os.environ overlaid with fleet.env (the file is authoritative, see fleet_view_server's
    read_env_values), minus the sign-in keys."""
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    for k in SECRET_ENV:
        env.pop(k, None)
    return env


def prompt(who: str, question: str, history: list) -> str:
    lines = [f"{who} is asking from the fleet dashboard."]
    for turn in history[-MAX_HISTORY:]:
        if isinstance(turn, dict):
            lines += [f"Earlier question: {str(turn.get('q', ''))[:2000]}",
                      f"Your earlier answer: {str(turn.get('a', ''))[:2000]}"]
    lines.append(f"Question: {question}")
    return "\n\n".join(lines)


def system_prompt(env: dict) -> str:
    return (
        "You answer questions about this fleet of AI agents for a signed-in operator, from the "
        "fleet's web dashboard. Write plain, short English for a QA professional, not a coder. "
        f"Fleet code and member specs: {KIT_DIR} (README.md, RUNBOOK.md, members/). "
        f"Logs and run records: {LOG_DIR} (runs.jsonl, <member>.log, writes.jsonl). "
        f"The repo the fleet works on: {env.get('FLEET_REPO', '')}. "
        f"Live data and actions go through: python3 {ACT} get /api/<route> or "
        f"python3 {ACT} post /api/<route> '<json>'. Useful reads: /api/members, "
        "/api/query?member=&status=&limit=, /api/stats/runs_summary, /api/spend, /api/next_fires, "
        "/api/fleet_state, /api/pass_log?member=. Writes (each is logged under the asker's name): "
        "/api/prune {issue,note}, /api/members/<name>/pause, /api/members/<name>/resume, "
        "/api/members/<name>/command {text}, /api/run_now {member}, /api/steer {member,key,value,why}, "
        "/api/fleet_toggle, /api/fleet_settings. Before any write, say what you will do; do it only "
        "if the operator asked for that change. You cannot read secrets or change code; say so if asked."
    )


def command(env: dict, text: str) -> list[str]:
    model = env.get("FLEET_CHAT_MODEL") or "sonnet"
    claude = [
        "claude", "-p", text, "--model", model, "--restricted",
        "--tools", "Read,Grep,Glob,Bash",
        "--allowedTools", f"Bash(python3 {ACT}:*)",
        "--settings", json.dumps({"permissions": {"deny": DENY}}),
        "--strict-mcp-config", "--max-turns", "30", "--output-format", "json",
        "--add-dir", str(LOG_DIR),
        *(["--add-dir", env["FLEET_REPO"]] if env.get("FLEET_REPO") else []),
        "--append-system-prompt", system_prompt(env),
    ]
    pool = KIT_DIR / "scripts" / "account_pool.sh"
    return ["bash", "-c", '. "$0" && account_pool_run "$@"', str(pool), "timeout", str(TIMEOUT_S), *claude]


def _answer(out: str) -> str:
    """The `result` of claude's JSON output. account_pool_run merges stderr into stdout, so
    take the last line that parses as a result object."""
    for line in reversed(out.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj and not obj.get("is_error"):
            return str(obj.get("result") or "")
    return ""


def _run(job_id: str) -> None:
    job = JOBS[job_id]
    env = _env()
    env["FLEET_CHAT_WHO"] = job["who"]
    try:
        p = subprocess.run(command(env, prompt(job["who"], job["question"], job["history"])),
                           cwd=str(KIT_DIR), env=env, capture_output=True, text=True,
                           timeout=TIMEOUT_S + 60)
        answer = _answer(p.stdout)
        status, error = ("done", "") if answer else ("failed", (p.stdout + p.stderr).strip()[-500:] or f"exit {p.returncode}")
    except subprocess.TimeoutExpired:
        answer, status, error = "", "failed", f"no answer within {TIMEOUT_S}s"
    except OSError as exc:
        answer, status, error = "", "failed", str(exc)
    with LOCK:
        job.update(status=status, answer=answer, error=error, finished=time.time())
    try:
        with (LOG_DIR / "chat.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "who": job["who"], "question": job["question"],
                                 "status": status, "answer": answer, "error": error}) + "\n")
    except OSError:
        pass


def start(who: str, question: str, history: list) -> str:
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION:
        raise ValueError(f"question too long (max {MAX_QUESTION} chars)")
    now = time.time()
    with LOCK:
        for k in [k for k, j in JOBS.items() if j.get("finished") and now - j["finished"] > KEEP_S]:
            del JOBS[k]
        if any(j["who"] == who and j["status"] == "running" for j in JOBS.values()):
            raise ValueError("one question at a time -- wait for the answer")
        job_id = secrets.token_hex(8)
        JOBS[job_id] = {"who": who, "question": question,
                        "history": history if isinstance(history, list) else [],
                        "status": "running", "answer": "", "error": "", "started": now}
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def get(job_id: str) -> dict | None:
    with LOCK:
        job = JOBS.get(job_id)
        return {k: v for k, v in job.items() if k != "history"} if job else None


if __name__ == "__main__":
    print(json.dumps(command(_env(), sys.argv[1] if len(sys.argv) > 1 else "hi"), indent=1))
