# Running the fleet on other model tokens

The fleet was built on one thing: `claude -p` signed in with a Claude subscription. That is
still the default and nothing here changes it. This page is how to bring a different kind of
token, what runs on it, and what does not yet.

| You have | What runs on it today | How it signs in |
|---|---|---|
| A Claude subscription login | every member (unchanged) | `claude setup-token`, as before |
| An Anthropic API key | every member | `ANTHROPIC_API_KEY_<ACCOUNT>` in fleet.env |
| An OpenAI API key | the reviewer (judge-judy) only | `OPENAI_API_KEY_<ACCOUNT>` in fleet.env |
| A ChatGPT login for Codex | the reviewer (judge-judy) only | `codex login` saved per account |

Nothing is on until you set it. With none of the settings below, every pass starts `claude`
with the same arguments and environment it always did (`scripts/test_providers.py` checks this
against a recording stand-in for the CLI).

## A hosted instance that other people use: API keys only

If more than one person brings tokens to the same instance, accept **API keys only**. Do not
collect anyone's Claude or ChatGPT *login*.

- Anthropic says so directly. From [Claude Code: Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
  (read 2026-10-01): "Anthropic does not permit third-party developers to offer Claude.ai
  login into their own applications, or to route requests through Free, Pro, or Max plan
  credentials on behalf of their users. Moreover, developers may not collect, store, or
  intermediate Claude.ai credentials or session tokens." The same page says developers
  "should use API key authentication", and that a key's usage must be "billed to the key
  owner" and not resold.
- OpenAI: we could not load OpenAI's terms page while writing this, so no claim is made here
  about what they allow. Check the provider's terms before putting anyone else's ChatGPT login
  on a shared box: <https://openai.com/policies/terms-of-use/>. Their Codex docs recommend an
  API key for automation and say to treat a saved ChatGPT login file like a password.

Your own subscription login on your own instance is the case the fleet has always run on.

## Anthropic API key

An account in `FLEET_ACCOUNTS` becomes a pay-per-token account when fleet.env has a key for it.
The variable is named like the existing per-account token variable
(`CLAUDE_CODE_OAUTH_TOKEN_<ACCOUNT>`): the account name upper-cased, `-` turned into `_`.

```sh
FLEET_ACCOUNTS="philanthropy apikey"
ANTHROPIC_API_KEY_APIKEY="sk-ant-..."
```

What the account pool (`scripts/account_pool.sh`) does with it:

- The key is set as `ANTHROPIC_API_KEY` on that account's `claude` call only. The ambient
  `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN` are cleared for that call. In `-p` mode
  the CLI always uses the key when it is present
  ([Authentication precedence](https://code.claude.com/docs/en/authentication)).
- If the pool fails over to another account, the key does not go with it.
- A key has no weekly limit, so the "resets at ..." gate does not apply. Out of credit
  ("credit balance is too low") gates the account for five minutes and then it is tried
  again. A bad key is treated like a logged-out account: gated for an hour, logged in
  `account-pool.log`.
- `account_status.sh` reports it as an API-key account (`--live` tests the key).
  `account_heartbeat.sh` skips it, because there is no login to expire.
- The run record gets `"provider": "claude", "auth": "api_key"`.

Not changed: the per-account variables themselves sit in the fleet's environment, exactly as
the per-account subscription tokens already do. A member with a shell can read its own
environment. Keeping keys out of members' reach is separate work (see follow-ups).

Order: the pool's "soonest weekly reset first" ordering reads a subscription meter. An API-key
account has no meter, so it keeps its place in `FLEET_ACCOUNTS`.

## Codex (OpenAI)

### What runs there, and why so little

A member's allow/deny tool lists are its authority. `claude` takes them as flags. `codex exec`
has a sandbox with three modes (`read-only`, `workspace-write`, `danger-full-access`) and
on/off switches for whole tools. It has no rule like "may run Bash, but never `git push
--force`". So `scripts/provider.py` (`tool_policy`) decides, per member:

- **Allowed:** a member with no tools at all (`"allow": ["none"]`) whose runner knows how to
  call Codex. Today that is judge-judy. It runs with the shell tool, web search, apps, plugins
  and sub-agents switched off, no user config (so no MCP servers), a read-only sandbox, in an
  empty directory.
- **Refused:** every member that is allowed a tool (minion, gru, the-fixer, ...). Running it
  on Codex would drop its deny rules. A refused member does not run; `run_member.sh` writes a
  `dispatch_skipped` run record whose evidence says why, and spends nothing.

This is the useful first step anyway: a reviewer from a different vendor than the builder.

### Turn it on

```sh
FLEET_PROVIDER_JUDGE_JUDY=codex
FLEET_CODEX_ACCOUNTS="openai"          # names must not repeat a FLEET_ACCOUNTS name
OPENAI_API_KEY_OPENAI="sk-..."         # API key account
```

or, for a ChatGPT login instead of a key (your own instance only):

```sh
FLEET_CODEX_ACCOUNTS="chatgpt"
# once, by hand:  CODEX_HOME="$HOME/.codex-chatgpt" codex login
```

The `codex` binary must be on the PATH where the fleet runs. **The container image does not
install it yet**, and the deploy scripts do not mount `~/.codex-<account>` yet, so today this
works on a host run; the image change is a follow-up.

Which provider a member uses, most specific first: `FLEET_PROVIDER_<MEMBER>` in fleet.env, then
`llm.provider` in the member's spec, then `FLEET_PROVIDER` in fleet.env, then claude. An unknown
name is refused, never treated as claude.

### How the call is made

`scripts/codex_pass.py` runs:

```
codex exec --json --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules
  --sandbox read-only --disable shell_tool --disable apps --disable plugins
  --disable multi_agent --disable browser_use --disable computer_use
  --disable image_generation --disable memories -c web_search="disabled"
  -C <empty dir> -m <model> -o <file> --output-schema <schema file> -
```

The prompt goes in on stdin. The verdict schema judge-judy passes to claude as `--json-schema`
goes to Codex as `--output-schema`. The answer is then checked by the same
`scripts/judge_judy_verdict.py`; an answer that is not a valid verdict is a strike and no
status is posted, as with claude.

Where each fact came from:

- Flags: `codex exec --help` and `codex features list` on the installed CLI, codex-cli 0.144.3,
  2026-10-01. An unknown feature name makes the CLI exit at once, so a renamed switch fails
  the pass instead of leaving a tool on.
- Event format (`thread.started`, `turn.completed` with `usage`, `item.completed` with an
  `agent_message`, `turn.failed`): the CLI's source,
  [codex-rs/exec/src/exec_events.rs](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs).
- Non-interactive mode, `--output-schema`, `CODEX_API_KEY` for one call, read-only default:
  <https://developers.openai.com/codex/noninteractive>.
- Config keys (`sandbox_mode`, `features.shell_tool`, `web_search`, `mcp_servers`):
  <https://developers.openai.com/codex/config-reference>. Codex does support MCP servers
  (`codex mcp`); the fleet's capability slots are not wired to it yet.
- Auth: an API-key account gets `CODEX_API_KEY` on its own call. A login account gets
  `CODEX_HOME=$HOME/.codex-<account>`. The codex process is given a short list of environment
  variables, not the fleet's whole environment.

### What has and has not been proven

Proven against the real CLI (codex-cli 0.144.3, 2026-10-01): every flag above is accepted, and
**one real review-shaped call** through `codex_pass.py` on a ChatGPT login came back with a
verdict that `judge_judy_verdict.py` accepted, with real token counts (model `gpt-5.6-terra`,
the verdict schema as sent, 7 seconds). A second real call showed a limit worth knowing: on a
ChatGPT login the fast-tier model was refused ("The 'gpt-5.4-mini' model is not supported when
using Codex with a ChatGPT account"), so keep the reviewer on `sonnet`/`opus` there or name a
model your plan has.

Not proven with a real call: an **OpenAI API key** account (no key was available; the key path
is tested against a stand-in that speaks the event format in the CLI's source), a real
out-of-credit or bad-key failure from OpenAI, and the whole of judge-judy.sh posting a Codex
verdict to a real PR. Run one review by hand before relying on it:
`FLEET_PROVIDER_JUDGE_JUDY=codex bash members/judge-judy/judge-judy.sh <pr>`.

## Model names and cost

Member specs keep saying `sonnet`, `opus`, `haiku`. On claude the name is passed as written.
On Codex the name maps through `scripts/provider_config.json`: `haiku` -> fast, `sonnet` ->
standard, `opus` -> smart, and each tier names a Codex model. Any other name is passed to the
CLI as written, so `FLEET_CODE_REVIEW_MODEL=gpt-5.5` works.

Codex does not report a dollar cost. `codex_pass.py` computes one from the price table in the
same file (prices read from <https://developers.openai.com/api/docs/pricing> on 2026-10-01;
edit them when they change). A model with no price is booked with cost **unknown** (null),
never zero, and so are token counts the CLI did not report. One thing to know: judge-judy's
per-tick spend cap counts an unknown cost as $0, so the cap only binds for priced models.

Every run record from a non-default pass carries `provider` and `auth` (`subscription`,
`api_key`, `chatgpt_login`), so cost per vendor can be compared. A record without them is a
claude pass on a subscription.

## Adding another provider

1. Add it to `scripts/provider_config.json`.
2. Teach `tool_policy()` in `scripts/provider.py` which member shapes it can really enforce.
   When unsure, refuse.
3. Write its `<name>_pass.py` that returns the envelope `pass_accounting.py` reads.
4. Add its account branch in `account_pool_run`.
