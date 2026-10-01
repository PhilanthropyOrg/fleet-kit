#!/usr/bin/env python3
"""The fleet can run on an Anthropic API key or on Codex, not only Claude logins.

Every test here runs the real scripts against a FAKE `claude` / `codex` on PATH that writes
down the arguments and environment it was started with. Nothing reaches a model.

What is proven, and how far:
  * the everyday path -- a member on a Claude subscription -- starts `claude` with exactly the
    arguments it always did, through the real run_member.sh (needs bash >= 4.4, see
    _bash_ok) and through judge-judy.sh's own call block (the block is lifted out of the
    script and run; the gh-driven PR picking around it is not run here);
  * an API-key account's key reaches that account's call and no other account's;
  * a member whose tool rules Codex cannot enforce is refused there, with a run record;
  * codex_pass.py turns Codex's documented event stream into the envelope the fleet's
    accounting and verdict readers already read. The fake codex speaks the format in the
    CLI's source (codex-rs/exec/src/exec_events.rs); no real Codex answer is involved.

Plain python, no pytest: `python3 scripts/test_providers.py`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(HERE))

import judge_judy_verdict  # noqa: E402
import member_spec  # noqa: E402
import pass_accounting  # noqa: E402
import provider  # noqa: E402

RECORD_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CODEX_HOME",
              "CODEX_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "IS_SANDBOX")

# Records one JSON file per call: argv, the env vars above, stdin. Then behaves as told by
# FAKE_MODE (per binary) -- by default a successful answer in that CLI's own output format.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
name = os.path.basename(sys.argv[0])
# codex_pass.py hands codex a stripped environment, so the fake finds its box by its own path
box = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))
out = os.path.join(box, "calls")
os.makedirs(out, exist_ok=True)
stdin = "" if sys.stdin.isatty() or name == "claude" else sys.stdin.read()
workdir = sys.argv[sys.argv.index("-C") + 1] if "-C" in sys.argv else None
rec = {"bin": name, "argv": sys.argv[1:], "stdin": stdin,
       "workdir_entries": os.listdir(workdir) if workdir else None,
       "env": {k: os.environ[k] for k in %(keys)r if k in os.environ}}
json.dump(rec, open(os.path.join(out, "%%s.%%d.%%d.json" %% (name, time.time_ns(), os.getpid())), "w"))
mode = open(os.path.join(box, "mode")).read() if os.path.exists(os.path.join(box, "mode")) else ""
verdict = {"verdict": "approve", "findings": []}
if name == "claude":
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if mode == "broke" and os.environ.get("ANTHROPIC_API_KEY"):
        print("API Error: Your credit balance is too low to access the Anthropic API.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"type": "result", "subtype": "success", "num_turns": 1, "stop_reason": "end_turn",
                      "total_cost_usd": 0.01, "usage": {"input_tokens": 3, "output_tokens": 2},
                      "structured_output": verdict,
                      "result": "Outcome: QUIET -- nothing to do\nEvidence: the fake claude ran\nSelf-critique: none"}))
    sys.exit(0)
# codex: the JSONL events of `codex exec --json`
def emit(e): print(json.dumps(e), flush=True)
emit({"type": "thread.started", "thread_id": "t-1"})
emit({"type": "turn.started"})
if mode == "codex_fails":
    emit({"type": "error", "message": "Reconnecting... 5/5 (unexpected status 401 Unauthorized)"})
    emit({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "reading the diff"}})
text = "I think it is fine." if mode == "codex_prose" else json.dumps(verdict)
emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
if mode == "codex_no_usage":
    sys.exit(0)
emit({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                          "output_tokens": 50, "reasoning_output_tokens": 20}})
''' % {"keys": RECORD_ENV}


class Box:
    """A throwaway instance: fake binaries, a HOME, a log dir, and the calls they recorded."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet_prov_"))
        (self.tmp / "bin").mkdir()
        for name in ("claude", "codex"):
            p = self.tmp / "bin" / name
            p.write_text(FAKE)
            p.chmod(0o755)
        gh = self.tmp / "bin" / "gh"
        gh.write_text("#!/bin/sh\nexit 1\n")
        gh.chmod(0o755)
        for d in ("home", "logs", "calls"):
            (self.tmp / d).mkdir()

    def env(self, **extra) -> dict:
        env = {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "HOME": str(self.tmp / "home"),
               "TMPDIR": str(self.tmp), "FLEET_LOG_DIR": str(self.tmp / "logs")}
        (self.tmp / "mode").write_text(extra.pop("FAKE_MODE", ""))   # how the fakes behave this run
        env.update(extra)
        return env

    def calls(self, name: str | None = None) -> list[dict]:
        recs = [json.loads(p.read_text()) for p in sorted((self.tmp / "calls").glob("*.json"))]
        return [r for r in recs if name is None or r["bin"] == name]

    def clear(self):
        shutil.rmtree(self.tmp / "calls")
        (self.tmp / "calls").mkdir()

    def runs(self) -> list[dict]:
        p = self.tmp / "logs" / "runs.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _spec(member: str) -> dict:
    return json.loads((KIT / "members" / member / f"{member}.fleet.json").read_text())


def _bash_ok() -> bool:
    """run_member.sh expands an empty array under `set -u` at its `claude -p` call. bash older
    than 4.4 (macOS ships 3.2) treats that as an error, on main as much as here, so the
    end-to-end member tests can only run where the fleet itself can: bash >= 4.4 (CI, the box)."""
    v = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                       capture_output=True, text=True).stdout.strip().split(".")
    return (int(v[0]), int(v[1])) >= (4, 4)


def _run_member(box: Box, member: str, fleet_env: str = "", **extra) -> subprocess.CompletedProcess:
    """A real run_member.sh pass in a throwaway instance with its own repo and origin."""
    origin, repo = box.tmp / "origin.git", box.tmp / "repo"
    if not repo.exists():
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
        subprocess.run(git + ["-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    env_file = box.tmp / "fleet.env"
    env_file.write_text(f'FLEET_REPO={repo}\nFLEET_LOG_DIR={box.tmp / "logs"}\n'
                        f'FLEET_ACCOUNTS="alpha"\n{fleet_env}')
    return subprocess.run(["bash", str(HERE / "run_member.sh"), member], cwd=box.tmp,
                          env=box.env(FLEET_ENV_FILE=str(env_file), **extra),
                          capture_output=True, text=True, timeout=300)


def _pool(box: Box, script: str, **extra) -> subprocess.CompletedProcess:
    full = f'set -u\n. "{HERE / "account_pool.sh"}"\n{script}\n'
    return subprocess.run(["bash", "-c", full], env=box.env(**extra), capture_output=True, text=True,
                          timeout=120)


# --- 1. the everyday path is unchanged ----------------------------------------------------

def _golden_member_argv(member: str) -> list[str]:
    """What origin/main's run_member.sh passes after `-p <prompt>` for a member with no caps
    and no capability slots -- written out here from that call, not computed by new code."""
    tools = _spec(member)["llm"]["tools"]
    return ["--model", _spec(member)["llm"]["model"], "--dangerously-skip-permissions",
            "--setting-sources", "user", "--output-format", "stream-json", "--verbose",
            "--allowedTools", " ".join(tools["allow"]), "--disallowedTools", " ".join(tools["deny"])]


def test_default_member_pass_starts_claude_exactly_as_before() -> None:
    if not _bash_ok():
        print("skip  member pass through run_member.sh -- needs bash >= 4.4 (see _bash_ok)")
        return
    box = Box()
    try:
        # The prompt itself is not compared run to run: it carries the time and the instance's
        # run history. It must hold the member's charter; nothing in this change builds it.
        charter = (KIT / "members" / "gru" / "gru.md").read_text().split("---", 2)[2].strip()
        for label, fleet_env in (("nothing set", ""), ("FLEET_PROVIDER=claude", "FLEET_PROVIDER=claude\n"),
                                 ("FLEET_PROVIDER_GRU=claude", "FLEET_PROVIDER_GRU=claude\n")):
            box.clear()
            r = _run_member(box, "gru", fleet_env, CLAUDE_CODE_OAUTH_TOKEN="ambient-token")
            calls = box.calls()
            assert r.returncode == 0 and len(calls) == 1 and calls[0]["bin"] == "claude", \
                (label, r.returncode, r.stderr[-800:], calls)
            argv, env = calls[0]["argv"], calls[0]["env"]
            assert argv[0] == "-p" and charter[:400] in argv[1], (label, argv[1][:200])
            assert argv[2:] == _golden_member_argv("gru"), (label, argv[2:])
            # a named subscription account: its own config dir, the ambient token cleared, no key
            assert env.get("CLAUDE_CONFIG_DIR", "").endswith("alpha"), env
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env, env
        last = box.runs()[-1]
        assert last["status"] != "started" and "provider" not in last and "auth" not in last, last
        print("ok  a subscription member pass starts claude with the same argv, env and run record "
              "(provider unset, FLEET_PROVIDER=claude, FLEET_PROVIDER_GRU=claude)")
    finally:
        box.close()


def _judge_block() -> tuple[str, str]:
    """judge-judy.sh's model call, lifted out of the script: from where it hands the pool a
    file for the picked account to where it works out the receipt. Plus its verdict schema."""
    src = (KIT / "members" / "judge-judy" / "judge-judy.sh").read_text()
    block = re.search(r"^  ACCOUNT_POOL_SELECTED_FILE=\$\(mktemp.*?^  REVIEW_RECEIPT=\"\".*?^  fi$",
                      src, re.M | re.S)
    schema = re.search(r"^VERDICT_SCHEMA='(.*)'$", src, re.M)
    assert block and schema, "judge-judy.sh's model call block or VERDICT_SCHEMA was not found"
    return block.group(0), schema.group(1)


def _judge(box: Box, provider_name: str, **extra) -> dict:
    block, schema = _judge_block()
    out = box.tmp / "judge.out"
    script = (f'KIT_DIR="{KIT}"; LOG="{box.tmp}/judge.log"; log() {{ echo "$*" >> "$LOG"; }}\n'
              f'PR=7; MODEL=sonnet; TIMEOUT_S=60; PROMPT="review this diff"\n'
              f"VERDICT_SCHEMA='{schema}'\nREVIEW_PROVIDER={provider_name}\n{block}\n"
              f'printf \'%s\' "$RAW" > "{out}"; echo "RC=$RC"; echo "RECEIPT=$REVIEW_RECEIPT"\n')
    r = _pool(box, script, **extra)
    return {"stdout": r.stdout, "stderr": r.stderr, "raw": out.read_text() if out.exists() else "",
            "schema": schema}


def test_judge_judy_default_call_is_unchanged() -> None:
    box = Box()
    try:
        got = _judge(box, "claude", FLEET_ACCOUNTS="alpha")
        calls = box.calls()
        assert "RC=0" in got["stdout"] and [c["bin"] for c in calls] == ["claude"], (got, calls)
        # origin/main: claude -p "$PROMPT" --model "$MODEL" --output-format json
        #              --json-schema "$VERDICT_SCHEMA" --max-budget-usd "${FLEET_MAX_BUDGET_USD:-5}"
        assert calls[0]["argv"] == ["-p", "review this diff", "--model", "sonnet", "--output-format", "json",
                                    "--json-schema", got["schema"], "--max-budget-usd", "5"], calls[0]["argv"]
        assert "RECEIPT=\n" in got["stdout"], got["stdout"]   # subscription: the run record is unchanged
        assert judge_judy_verdict.parse(got["raw"])["ok"]
        print("ok  judge-judy on claude: same argv as before, no codex call, no receipt flags")
    finally:
        box.close()


# --- 2. Anthropic API-key accounts ---------------------------------------------------------

def test_api_key_reaches_its_own_account_only() -> None:
    box = Box()
    try:
        r = _pool(box, 'account_pool_run claude -p hi; echo "RC=$?"',
                  FLEET_ACCOUNTS="paid sub", ANTHROPIC_API_KEY_PAID="sk-ant-test-123",
                  CLAUDE_CODE_OAUTH_TOKEN="ambient-token", FAKE_MODE="broke",
                  ACCOUNT_POOL_SELECTED_FILE=str(box.tmp / "selected"))
        calls = box.calls("claude")
        assert "RC=0" in r.stdout and len(calls) == 2, (r.stdout, r.stderr, calls)
        paid, sub = calls
        assert paid["env"].get("ANTHROPIC_API_KEY") == "sk-ant-test-123", paid["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in paid["env"], "the subscription token rode along with the key"
        assert paid["env"]["CLAUDE_CONFIG_DIR"].endswith("paid"), paid["env"]
        assert "ANTHROPIC_API_KEY" not in sub["env"], "the key followed the failover to another account"
        assert sub["env"]["CLAUDE_CONFIG_DIR"].endswith("sub"), sub["env"]
        assert (box.tmp / "selected").read_text().strip() == "sub"
        # out of credit is not a weekly limit: gated for the 5-minute fallback, not a week
        log = (box.tmp / "logs" / "account-pool.log").read_text()
        assert "account=paid command failed rc=1 reason=exhausted" in log, log
        epoch = int((box.tmp / "logs" / "account-pool-exhausted.state").read_text().split()[1])
        import time
        assert 0 < epoch - time.time() <= 310, epoch - time.time()

        kinds = _pool(box, 'account_pool_auth_kind paid; account_pool_auth_kind sub; '
                           'account_pool_auth_kind oai codex; account_pool_auth_kind chat codex',
                      ANTHROPIC_API_KEY_PAID="k", OPENAI_API_KEY_OAI="k").stdout.split()
        assert kinds == ["api_key", "subscription", "api_key", "chatgpt_login"], kinds
        # the same words from a subscription account are NOT a limit -- classified as before
        words = "Your credit balance is too low"
        cls = _pool(box, f'_account_pool_classify_failure "{words}" 1 subscription; '
                         f'_account_pool_classify_failure "{words}" 1; '
                         f'_account_pool_classify_failure "{words}" 1 api_key').stdout.split()
        assert cls == ["other", "other", "exhausted"], cls
        print("ok  an API-key account's key is on its own call only; out-of-credit gates it for minutes")
    finally:
        box.close()


def test_api_key_pass_says_so_on_its_run_record() -> None:
    if not _bash_ok():
        print("skip  API-key member pass through run_member.sh -- needs bash >= 4.4 (see _bash_ok)")
        return
    box = Box()
    try:
        r = _run_member(box, "gru", "ANTHROPIC_API_KEY_ALPHA=sk-ant-test-123\n")
        call = box.calls("claude")[0]
        assert r.returncode == 0 and call["env"].get("ANTHROPIC_API_KEY") == "sk-ant-test-123", (r.stderr[-500:], call["env"])
        assert call["argv"][2:] == _golden_member_argv("gru"), call["argv"][2:]
        last = box.runs()[-1]
        assert (last.get("provider"), last.get("auth")) == ("claude", "api_key"), last
        print("ok  a member pass on an API-key account: same argv, key in env, provider/auth on the record")
    finally:
        box.close()


# --- 3. tool-policy honesty ---------------------------------------------------------------

def test_tool_policy_on_real_member_specs() -> None:
    for path in sorted((KIT / "members").glob("*/*.fleet.json")):
        spec = json.loads(path.read_text())
        assert provider.tool_policy(spec, "claude")["ok"], path.name   # claude enforces every spec
    judy = provider.tool_policy(_spec("judge-judy"), "codex")
    assert judy["ok"] and judy["mode"] == "no_tools", judy
    minion = provider.tool_policy(_spec("minion"), "codex")
    assert not minion["ok"] and "Bash(git stash:*)" in minion["reason"], minion
    for member in ("gru", "the-fixer", "librarian", "nerd"):
        assert not provider.tool_policy(_spec(member), "codex")["ok"], member
    # no tools, but its runner only calls claude -- allowed nowhere it would not really run
    scrub = provider.tool_policy(_spec("librarian-scrub"), "codex")
    assert not scrub["ok"] and "runner" in scrub["reason"], scrub
    # a builder with no deny rules is still refused: Codex cannot hold it to a tool list
    loose = _spec("gru")
    loose["llm"]["tools"]["deny"] = []
    assert not provider.tool_policy(loose, "codex")["ok"]
    assert not provider.tool_policy(_spec("judge-judy"), "gemini")["ok"]
    print("ok  on codex: the no-tools reviewer is allowed; minion and every tool-using member is refused")


def test_a_refused_member_never_runs_and_leaves_a_record() -> None:
    box = Box()
    try:
        r = _run_member(box, "gru", "FLEET_PROVIDER_GRU=codex\n")
        assert r.returncode == 0, r.stderr[-800:]
        assert box.calls() == [], f"a model was started for a refused member: {box.calls()}"
        rows = box.runs()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["status"] == "dispatch_skipped" and row["provider"] == "codex", row
        assert "cannot run on codex" in row["evidence"] and "Bash(git stash:*)" in row["evidence"], row
        assert row["tokens"]["cost_usd"] is None
        assert "REFUSED" in (box.tmp / "logs" / "gru.log").read_text()

        box.clear()
        r = _run_member(box, "gru", "FLEET_PROVIDER=codx\n")   # a typo is refused too, never claude
        assert box.calls() == [] and box.runs()[-1]["status"] == "dispatch_skipped", (r.stderr[-300:], box.runs())
        print("ok  gru sent to codex: no model started, one dispatch_skipped row carrying the reason")
    finally:
        box.close()


# --- 4. the Codex adapter -----------------------------------------------------------------

def _codex_pass(box: Box, model: str = "sonnet", schema: str | None = None, **extra):
    prompt = box.tmp / "prompt.txt"
    prompt.write_text("review this diff\n+ a line")
    cmd = [sys.executable, str(HERE / "codex_pass.py"), "--model", model, "--prompt-file", str(prompt)]
    if schema:
        cmd += ["--schema", schema]
    return subprocess.run(cmd, env=box.env(**extra), capture_output=True, text=True, timeout=60)


def test_codex_pass_flags_and_envelope() -> None:
    box = Box()
    try:
        schema = _judge_block()[1]
        r = _codex_pass(box, schema=schema, CODEX_API_KEY="sk-oai-test", GH_TOKEN="ghp_secret",
                        ANTHROPIC_API_KEY="sk-ant-secret")
        assert r.returncode == 0 and r.stderr == "", (r.returncode, r.stderr)
        call = box.calls("codex")[0]
        argv = call["argv"]
        assert argv[:2] == ["exec", "--json"] and argv[-1] == "-", argv
        assert call["stdin"] == "review this diff\n+ a line"          # the prompt goes by stdin
        assert argv[argv.index("--sandbox") + 1] == "read-only", argv
        assert argv[argv.index("-m") + 1] == "gpt-5.6-terra", argv    # sonnet -> standard tier
        for flag in ("--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"):
            assert flag in argv, flag
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        assert {"shell_tool", "apps", "plugins", "multi_agent"} <= disabled, disabled
        assert 'web_search="disabled"' in argv
        assert "danger-full-access" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert call["workdir_entries"] == [], call["workdir_entries"]   # it runs in an empty directory
        # only its own credential crosses into the codex process
        assert call["env"] == {"CODEX_API_KEY": "sk-oai-test"}, call["env"]

        env = json.loads(r.stdout)
        text, usage = pass_accounting.split(r.stdout)
        assert env["provider"] == "codex" and env["model"] == "gpt-5.6-terra"
        # codex counts the 400 cached tokens inside its 1000 input tokens; booked claude-style
        assert usage["input_tokens"] == 600 and usage["cache_read_input_tokens"] == 400, usage
        assert usage["output_tokens"] == 50 and usage["num_turns"] == 1, usage
        assert usage["cache_creation_input_tokens"] is None, usage
        want = (600 * 2.0 + 400 * 0.2 + 50 * 12.0) / 1_000_000
        assert abs(usage["total_cost_usd"] - want) < 1e-9, (usage["total_cost_usd"], want)
        verdict = judge_judy_verdict.parse(r.stdout)
        assert verdict["ok"] and verdict["verdict"] == "approve", verdict
        print("ok  codex_pass: read-only, tools off, own key only; envelope reads as claude's does")
    finally:
        box.close()


def test_codex_unknowns_stay_unknown_and_failures_fail_closed() -> None:
    box = Box()
    try:
        schema = _judge_block()[1]
        r = _codex_pass(box, model="gpt-9-not-in-the-table", schema=schema)
        usage = pass_accounting.split(r.stdout)[1]
        assert r.returncode == 0 and usage["total_cost_usd"] is None and usage["input_tokens"] == 600, usage

        r = _codex_pass(box, schema=schema, FAKE_MODE="codex_no_usage")
        usage = pass_accounting.split(r.stdout)[1]
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "total_cost_usd", "num_turns"):
            assert usage[key] is None, (key, usage)     # never 0 for "not reported"

        r = _codex_pass(box, schema=schema, FAKE_MODE="codex_prose")
        verdict = judge_judy_verdict.parse(r.stdout)
        assert r.returncode == 0 and not verdict["ok"], verdict   # prose is a strike, not an approve

        r = _codex_pass(box, schema=schema, FAKE_MODE="codex_fails")
        assert r.returncode != 0 and r.stdout == "" and "401 Unauthorized" in r.stderr, (r.returncode, r.stdout, r.stderr)
        print("ok  codex: unknown price/usage is null, a prose answer is no verdict, a failed turn is a failed call")
    finally:
        box.close()


def test_strict_schema_only_adds_additional_properties() -> None:
    import codex_pass
    schema = json.loads(_judge_block()[1])
    strict = codex_pass.strict_schema(schema)
    assert strict["additionalProperties"] is False
    assert strict["properties"]["findings"]["items"]["additionalProperties"] is False
    strip = lambda o: ({k: strip(v) for k, v in o.items() if k != "additionalProperties"} if isinstance(o, dict)  # noqa: E731
                       else [strip(v) for v in o] if isinstance(o, list) else o)
    assert strip(strict) == schema
    print("ok  the verdict schema sent to codex is the same schema, closed to extra keys")


# --- 5. judge-judy on another vendor ------------------------------------------------------

def test_judge_judy_reviews_on_codex_when_asked() -> None:
    box = Box()
    try:
        got = _judge(box, "codex", FLEET_ACCOUNTS="alpha", FLEET_CODEX_ACCOUNTS="oai",
                     OPENAI_API_KEY_OAI="sk-oai-test", CLAUDE_CODE_OAUTH_TOKEN="ambient-token")
        calls = box.calls()
        assert "RC=0" in got["stdout"] and [c["bin"] for c in calls] == ["codex"], (got, calls)
        assert calls[0]["env"] == {"CODEX_API_KEY": "sk-oai-test",
                                   "CODEX_HOME": str(box.tmp / "home" / ".codex-oai")}, calls[0]["env"]
        assert calls[0]["stdin"] == "review this diff"
        assert "--output-schema" in calls[0]["argv"]
        assert "RECEIPT=--provider codex --auth api_key" in got["stdout"], got["stdout"]
        verdict = judge_judy_verdict.parse(got["raw"])
        assert verdict["ok"] and verdict["verdict"] == "approve", verdict

        # a ChatGPT-login account: its CODEX_HOME, and no key of any kind
        box.clear()
        got = _judge(box, "codex", FLEET_CODEX_ACCOUNTS="chat", OPENAI_API_KEY="ambient-openai")
        assert box.calls("codex")[0]["env"] == {"CODEX_HOME": str(box.tmp / "home" / ".codex-chat")}
        assert "RECEIPT=--provider codex --auth chatgpt_login" in got["stdout"], got["stdout"]

        # codex asked for, no codex account configured: no call to ANY vendor, and it says why
        box.clear()
        got = _judge(box, "codex", FLEET_ACCOUNTS="alpha")
        assert "RC=3" in got["stdout"] and box.calls() == [], (got["stdout"], box.calls())
        # a failed codex call posts nothing parseable
        got = _judge(box, "codex", FLEET_CODEX_ACCOUNTS="oai", OPENAI_API_KEY_OAI="k", FAKE_MODE="codex_fails")
        assert "RC=0" not in got["stdout"] and not judge_judy_verdict.parse(got["raw"])["ok"], got
        assert box.calls("claude") == []
        log = (box.tmp / "logs" / "account-pool.log").read_text()
        assert "account=oai command failed rc=1 reason=unauthenticated" in log, log
        print("ok  judge-judy on codex: codex is called (never claude), verdict validates, receipt says codex/api_key")
    finally:
        box.close()


# --- 6. receipts, model names, the spec ---------------------------------------------------

def test_run_report_carries_provider_and_auth_only_when_told() -> None:
    def record(*flags: str) -> dict:
        out = subprocess.run([sys.executable, str(HERE / "run_report.py"), "--member", "m", "--run-id", "r",
                              "--pass-file", "-", *flags], input="Outcome: QUIET -- x\nEvidence: y\n",
                             capture_output=True, text=True, env={**os.environ, "FLEET_LOG_DIR": tempfile.gettempdir()})
        return json.loads(out.stdout)
    plain = record()
    assert "provider" not in plain and "auth" not in plain, plain
    told = record("--provider", "codex", "--auth", "api_key")
    assert (told["provider"], told["auth"]) == ("codex", "api_key")
    assert {k: v for k, v in told.items() if k not in ("provider", "auth", "ts")} == \
           {k: v for k, v in plain.items() if k != "ts"}
    print("ok  run records carry provider/auth when given and are unchanged when not")


def test_model_names_and_spec_stay_strict() -> None:
    for name in ("haiku", "sonnet", "opus", "claude-opus-4-1"):
        assert provider.model_for("claude", name) == name          # claude: always as written
    assert provider.model_for("codex", "haiku") == "gpt-5.4-mini"
    assert provider.model_for("codex", "opus") == "gpt-5.5"
    assert provider.model_for("codex", "gpt-5.6-luna") == "gpt-5.6-luna"   # the operator named one
    assert provider.cost_usd("codex", "nope", input_tokens=1, cached_input_tokens=0, output_tokens=1) is None
    assert provider.cost_usd("codex", "gpt-5.5", input_tokens=None, cached_input_tokens=0, output_tokens=1) is None

    spec = _spec("judge-judy")
    assert provider.resolve(spec, {}) == "claude"
    assert provider.resolve(spec, {"FLEET_PROVIDER": "codex"}) == "codex"
    assert provider.resolve(spec, {"FLEET_PROVIDER": "codex", "FLEET_PROVIDER_JUDGE_JUDY": "claude"}) == "claude"
    spec["llm"]["provider"] = "codex"
    member_spec.validate(spec)
    assert provider.resolve(spec, {"FLEET_PROVIDER": "claude"}) == "codex"   # the spec beats the instance default
    spec["llm"]["provider"] = "codx"
    try:
        member_spec.validate(spec)
    except member_spec.SpecError as e:
        assert "llm.provider" in str(e)
    else:
        raise AssertionError("member_spec accepted an unknown llm.provider")
    try:
        provider.resolve(_spec("gru"), {"FLEET_PROVIDER": "codx"})
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown FLEET_PROVIDER resolved to something")
    print("ok  old model names work everywhere; an unknown provider is an error, never a quiet claude")


if __name__ == "__main__":
    test_default_member_pass_starts_claude_exactly_as_before()
    test_judge_judy_default_call_is_unchanged()
    test_api_key_reaches_its_own_account_only()
    test_api_key_pass_says_so_on_its_run_record()
    test_tool_policy_on_real_member_specs()
    test_a_refused_member_never_runs_and_leaves_a_record()
    test_codex_pass_flags_and_envelope()
    test_codex_unknowns_stay_unknown_and_failures_fail_closed()
    test_strict_schema_only_adds_additional_properties()
    test_judge_judy_reviews_on_codex_when_asked()
    test_run_report_carries_provider_and_auth_only_when_told()
    test_model_names_and_spec_stay_strict()
