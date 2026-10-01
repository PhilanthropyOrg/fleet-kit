#!/usr/bin/env python3
"""provider -- which model CLI a member runs on, and whether it is safe to run it there.

The fleet was built on one CLI (`claude -p`). This is the one place that knows there can be
another: it reads scripts/provider_config.json and answers four questions for the shell
callers (run_member.sh, judge-judy.sh) and for codex_pass.py:

  resolve      which provider does this member run on?
  tool-policy  can that provider ENFORCE this member's allow/deny tool lists?
  model        what does the spec's model name ("sonnet") mean on that provider?
  cost         what did these tokens cost, from the price table? (None when unknown)

THE RULE THAT MATTERS (tool_policy): a member's allow/deny lists are its authority. claude
takes them as flags. Another CLI may have nothing like a per-command deny rule -- and a
member that runs there anyway runs UNGUARDED. So a provider that cannot enforce a member's
policy does not get that member: tool_policy() says no, with the reason, and the runner
records the refusal instead of spending anything. Never the other way round.

Usage:
  provider.py resolve --member judge-judy < spec.json        -> claude | codex
  provider.py tool-policy --provider codex < spec.json       -> {"ok":..,"mode":..,"reason":..}
                                                                exit 0 allowed, 1 refused
  provider.py model --provider codex --model sonnet          -> gpt-5.6-terra
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "provider_config.json"


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def providers() -> list[str]:
    return sorted(load_config()["providers"])


def _env_key(member: str) -> str:
    return "FLEET_PROVIDER_" + re.sub(r"[^A-Za-z0-9]", "_", member).upper()


def resolve(spec: dict, env: dict | None = None) -> str:
    """Most specific wins: FLEET_PROVIDER_<MEMBER> (this instance, this member), then the
    spec's own llm.provider, then FLEET_PROVIDER (this instance, every member), then the
    config's default (claude). An unknown name is an error, never a silent fall back to
    claude -- an operator who typed `codx` must find out, not quietly keep paying Anthropic."""
    env = os.environ if env is None else env
    cfg = load_config()
    name = (env.get(_env_key(spec.get("name", ""))) or (spec.get("llm") or {}).get("provider")
            or env.get("FLEET_PROVIDER") or cfg["default_provider"])
    if name not in cfg["providers"]:
        raise ValueError(f"unknown provider {name!r} (known: {sorted(cfg['providers'])})")
    return name


def tool_policy(spec: dict, provider: str) -> dict:
    """Can `provider` enforce this member's tool policy? -> {"ok", "mode", "reason"}.

    claude: always -- the lists go to the CLI as --allowedTools/--disallowedTools.

    codex: `codex exec` has a sandbox with three modes (read-only, workspace-write,
    danger-full-access) and on/off feature switches (shell_tool, web_search, ...). It has no
    per-tool allow list and no per-command deny rule. So exactly one member shape can be
    enforced today, and everything else is refused:

      mode "no_tools" -- llm.tools.allow is ["none"] (the reviewer: it reads a diff as text
        and answers). Enforced by switching the shell tool, web search, apps, plugins and
        sub-agents OFF, loading no user config (so no MCP servers), and a read-only sandbox
        in an empty directory. codex_pass.py builds those flags.

    Refused: any member that is allowed a tool. Its deny rules ("Bash(git push --force:*)")
    and its narrowed allows have no Codex equivalent, so it would run with more authority
    than its spec gives it. Also refused: a member with its own runner script that has not
    been taught to call this provider -- the runner would just call claude.
    """
    cfg = load_config()
    if provider not in cfg["providers"]:
        return {"ok": False, "mode": None, "reason": f"unknown provider {provider!r}"}
    pcfg = cfg["providers"][provider]
    if pcfg.get("tool_policy") == "native":
        return {"ok": True, "mode": "native", "reason": "allow/deny lists are passed to the CLI"}

    llm = spec.get("llm") or {}
    tools = llm.get("tools") or {}
    allow = [str(t) for t in tools.get("allow") or []]
    deny = [str(t) for t in tools.get("deny") or []]
    name = spec.get("name", "this member")

    def refuse(why: str) -> dict:
        return {"ok": False, "mode": None,
                "reason": f"{name} cannot run on {provider}: {why}"}

    if allow != ["none"]:
        scoped = [d for d in deny if "(" in d]
        if scoped:
            return refuse(f"its deny rule {scoped[0]!r} cannot be enforced -- {provider} has "
                          f"sandbox modes, not per-command rules, so the member would run "
                          f"without that limit")
        return refuse(f"it is allowed tools {allow} and {provider} cannot limit a run to a "
                      f"named tool list; only a member with no tools (allow: [\"none\"]) runs "
                      f"there")
    if llm.get("capabilities"):
        return refuse(f"it uses capability slots {llm['capabilities']} (MCP servers), which are "
                      f"not wired for {provider}")
    runner = llm.get("runner")
    if not runner:
        return refuse("a no-tools member needs its own runner script, and it has none")
    if runner not in (pcfg.get("runners") or []):
        return refuse(f"its runner {runner} only knows how to call claude")
    return {"ok": True, "mode": "no_tools",
            "reason": "no tools: shell, web search, apps, plugins and MCP are switched off "
                      "and the sandbox is read-only"}


def model_for(provider: str, model: str) -> str:
    """The spec's model name on this provider. claude: unchanged. Others: a tier name
    (haiku/sonnet/opus) maps through the provider's table; any other name is the operator
    naming that provider's own model and is passed as written."""
    cfg = load_config()
    pcfg = cfg["providers"][provider]
    models = pcfg.get("models")
    if not models:
        return model
    tier = None if model.startswith("_") else cfg["model_tiers"].get(model)
    if tier is None:
        return model
    return models[tier]


def cost_usd(provider: str, model: str, *, input_tokens: int | None, cached_input_tokens: int | None,
             output_tokens: int | None) -> float | None:
    """Price-table cost, or None when anything needed is unknown. `input_tokens` is the part
    of the input that was NOT read from cache; `cached_input_tokens` is the part that was. NEVER zero for unknown: a
    $0 row reads as "free" and drags every average down; a null row reads as "not known"."""
    prices = (load_config()["providers"][provider].get("prices_usd_per_mtok") or {}).get(model)
    if not isinstance(prices, dict) or input_tokens is None or output_tokens is None:
        return None
    cached = cached_input_tokens or 0
    try:
        total = (input_tokens * prices["input"]
                 + cached * prices.get("cached_input", prices["input"])
                 + output_tokens * prices["output"])
    except (KeyError, TypeError):
        return None
    return round(total / 1_000_000, 6)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--member")
    t = sub.add_parser("tool-policy")
    t.add_argument("--provider", required=True)
    m = sub.add_parser("model")
    m.add_argument("--provider", required=True)
    m.add_argument("--model", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "model":
        print(model_for(a.provider, a.model))
        return 0
    spec = json.load(sys.stdin)
    if a.cmd == "resolve":
        if a.member:
            spec.setdefault("name", a.member)
        try:
            print(resolve(spec))
        except ValueError as e:
            print(f"provider.py: {e}", file=sys.stderr)
            return 2
        return 0
    verdict = tool_policy(spec, a.provider)
    print(json.dumps(verdict))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
