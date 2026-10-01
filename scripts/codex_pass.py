#!/usr/bin/env python3
"""codex_pass -- run one no-tools model pass on OpenAI's Codex CLI and hand back the same
JSON envelope `claude -p --output-format json` returns, so pass_accounting.py,
judge_judy_verdict.py and run_report.py read it without knowing which vendor answered.

Every flag below was checked against the installed CLI (codex-cli 0.144.3, `codex exec
--help`, 2026-10-01) and the vendor's docs (docs/providers.md lists them). The event shapes
are the ones in the CLI's own source, codex-rs/exec/src/exec_events.rs.

  codex exec --json          events on stdout, one JSON object per line
  -                          the prompt is read from stdin (a review prompt carries a whole
                             diff; argv has a size limit, stdin does not)
  -m MODEL
  --output-schema FILE       the final answer must match this JSON Schema
  -o FILE                    the final answer, also written here
  --sandbox read-only        nothing on disk can be changed, no network from commands
  --disable shell_tool ...   the tools themselves are switched off (see NO_TOOLS)
  -c web_search="disabled"   removes the web search tool
  --ignore-user-config       no config.toml, so no MCP servers; login is still read
  --ignore-rules             no user/project exec rules
  --ephemeral                no session file left behind
  --skip-git-repo-check -C   run in an empty directory made for this one call

THIS RUNS THE NO-TOOLS SHAPE ONLY. provider.py's tool_policy() refuses every member that is
allowed a tool on Codex, so there is no flag here for a wider sandbox -- adding one is how a
member would end up running without its deny rules.

Auth is whatever the caller put in the environment (account_pool.sh does this per account):
CODEX_HOME=<dir with a ChatGPT login>, or CODEX_API_KEY=<key> for that one call. The child
gets a short allowlist of environment variables, not the fleet's whole environment.

Exit: 0 and one JSON object on stdout when the model answered. Otherwise codex's own exit
code (or 1), nothing on stdout, and the error text on stderr for account_pool.sh to classify.
Usage that the CLI did not report is null in the envelope -- unknown, never zero.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider  # noqa: E402

# Feature switches that give the model something to act with. All were accepted by the
# installed CLI; an unknown name makes codex exit at once ("Unknown feature flag"), which
# fails the pass closed rather than running with the tool still on.
NO_TOOLS = ("shell_tool", "apps", "plugins", "multi_agent", "browser_use", "computer_use",
            "image_generation", "memories")

# What the codex process may see. Everything else in the fleet's environment (GitHub tokens,
# other accounts' keys) stays out of it.
ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "CODEX_HOME", "CODEX_API_KEY",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
             "SSL_CERT_FILE", "SSL_CERT_DIR")


def build_argv(*, model: str, workdir: str, last_file: str, schema_file: str | None) -> list[str]:
    argv = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only"]
    for feature in NO_TOOLS:
        argv += ["--disable", feature]
    argv += ["-c", 'web_search="disabled"', "-C", workdir, "-m", model, "-o", last_file]
    if schema_file:
        argv += ["--output-schema", schema_file]
    argv.append("-")
    return argv


def strict_schema(schema):
    """OpenAI's structured output wants `additionalProperties: false` on every object; the
    fleet's schemas were written for claude and leave it out. Add it, change nothing else."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


def normalise(events: list[dict], *, model: str, duration_ms: int, want_json: bool) -> dict | None:
    """Codex's event list -> the claude-shaped envelope, or None if the model never answered."""
    text = None
    turns = 0
    usage: dict[str, int] | None = None
    for evt in events:
        etype = evt.get("type")
        item = evt.get("item") or {}
        if etype == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text")
        elif etype == "turn.completed":
            turns += 1
            u = evt.get("usage")
            if isinstance(u, dict):
                usage = usage or {}
                for k, v in u.items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0) + v
        elif etype == "turn.failed":
            return None
    if not isinstance(text, str) or not text.strip():
        return None

    # Codex counts cached tokens INSIDE input_tokens; claude reports them beside it. Book the
    # claude way so the two vendors' rows add up the same. Anything not reported stays null.
    total_in = (usage or {}).get("input_tokens")
    cached = (usage or {}).get("cached_input_tokens")
    out_tok = (usage or {}).get("output_tokens")
    fresh_in = total_in - (cached or 0) if total_in is not None else None
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "provider": "codex",
        "model": model,
        "result": text,
        "num_turns": turns or None,
        "stop_reason": None,
        "duration_ms": duration_ms,
        "usage": {
            "input_tokens": fresh_in,
            "output_tokens": out_tok,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": None,
        },
        "total_cost_usd": provider.cost_usd("codex", model, input_tokens=fresh_in,
                                            cached_input_tokens=cached, output_tokens=out_tok),
    }
    if want_json:
        # No structured_output key when the answer is not a JSON object: judge_judy_verdict.py
        # then tries `result` itself, fails, and counts a strike. Never a guessed verdict.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            envelope["structured_output"] = parsed
    return envelope


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="one no-tools pass on the Codex CLI")
    ap.add_argument("--model", required=True, help="a spec model name (sonnet) or a codex model slug")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--schema", help="JSON Schema (text) the final answer must match")
    a = ap.parse_args(argv)

    model = provider.model_for("codex", a.model)
    prompt = Path(a.prompt_file).read_text()
    with tempfile.TemporaryDirectory(prefix="fleet_codex_") as tmp:
        workdir = os.path.join(tmp, "empty")
        os.mkdir(workdir)
        last_file = os.path.join(tmp, "last.txt")
        schema_file = None
        if a.schema:
            schema_file = os.path.join(tmp, "schema.json")
            Path(schema_file).write_text(json.dumps(strict_schema(json.loads(a.schema))))
        cmd = build_argv(model=model, workdir=workdir, last_file=last_file, schema_file=schema_file)
        env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
        started = time.time()
        err_path = os.path.join(tmp, "stderr.txt")
        with open(err_path, "w") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                    env=env, text=True)

            # `timeout` signals THIS process; codex must go with it or it keeps spending.
            def _stop(signum, _frame):
                proc.kill()
                sys.exit(128 + signum)
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)

            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            events, notes = [], []
            for line in proc.stdout:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict):
                    continue
                events.append(evt)
                if evt.get("type") in ("error", "turn.failed"):
                    msg = evt.get("message") or (evt.get("error") or {}).get("message") or ""
                    notes.append(str(msg))
            rc = proc.wait()
        envelope = None
        if rc == 0:
            envelope = normalise(events, model=model, duration_ms=int((time.time() - started) * 1000),
                                 want_json=bool(a.schema))
        if envelope is None:
            tail = Path(err_path).read_text(errors="ignore")[-2000:]
            print(f"codex_pass: codex exec failed rc={rc} model={model}: "
                  + " | ".join(notes[-3:]) + "\n" + tail, file=sys.stderr)
            return rc or 1
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
