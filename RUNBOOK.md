# fleet-kit RUNBOOK — every lever on one page

The 2026-09-19 rebuild took a day because these facts lived in six logs, three handoffs and
one person's head. This page is the levers. `docs/deployment-learnings.md` is the why.
Everything below is for the `philanthropy` instance on `dino`; substitute your container name.

## 1. Is it alive? (run this first, always)

```
ssh dino 'bash ~/fleet-kit/scripts/fleet_status.sh'
```

One screen, one verdict per layer, top to bottom: container → deploy → cron → kill switch →
accounts → ceiling → runs → output. **The first `DEAD` is the bug.** `WAIT` means the fleet
is choosing not to work (budget, pacing, deploy window) and says why. Never diagnose from a
single log again.

## 2. Restart / recover

| Symptom | Do this |
|---|---|
| `DEAD container` | `bash ~/fleet-kit/scripts/refresh_container.sh philanthropy` (podman restart with the rootlessport retry) |
| `DEAD cron` (no tick >20 min) | Same command. A fresh container is the ONLY thing that has revived a wedged cron (fk#1171). Restarting the cron process inside does nothing; the watchdog already tried 24 times. |
| `WAIT deploy` for >2h after a merge | `tail ~/fleet-kit-logs/auto_deploy.log`. `ABORT: working tree dirty` = an untracked file in `~/fleet-kit`; commit or delete it. Force: `cd ~/fleet-kit && FLEET_INSTANCE_DIR=$PWD/instances/nonprofit-atlas FLEET_CONTAINER_NAME=philanthropy bash scripts/deploy.sh` |
| Roll back a bad deploy | `bash ~/fleet-kit/scripts/deploy.sh --rollback` (previous build is kept stopped as `philanthropy-retired`) |
| Stop everything | `FLEET_ENABLED=false` in `~/fleet-kit/instances/nonprofit-atlas/fleet.env` (read per run, no restart needed). `FLEET_BUILDER_ENABLED` / `FLEET_REVIEWER_ENABLED` for one lane. |

## 3. Kick one member now (don't wait for its cron slot)

```
podman exec -d philanthropy bash -c 'set -a; eval "$(grep -hE "^[A-Z_]+=" /etc/cron.d/*)"; set +a; \
  export GH_TOKEN=$(cat /root/.gh_token); cd /fleet-kit && bash scripts/run_member.sh <member> >> /var/log/fleet-kit/<member>.log 2>&1'
```

gru is `bash scripts/run_gru_fanout.sh` instead of `run_member.sh gru`. A second kick while one
runs logs `SKIP: another <member> pass already holds the dispatch lock` and exits; that is the
guard, not a failure. From outside the box, the fleet_view server has `POST /api/run_now`
(README "Driving the fleet from outside the box").

## 4. Verify a change is actually running

Container code is **baked into the image**; the host checkout proves nothing.

```
grep 'deploy OK at' ~/fleet-kit-logs/auto_deploy.log | tail -1        # must name your sha
podman exec philanthropy grep -c "<a string only your change has>" /fleet-kit/scripts/<file>
```

Merge → up to 2h (`FLEET_DEPLOY_MIN_INTERVAL_S`, auto_deploy coalesces) → `deploy OK` → next
cron slot. Budget three hours from merge to first evidence, not thirty minutes.

## 5. Members (manifest = `members/<name>/<name>.fleet.json`, instructions = `members/<name>/<name>.md`)

**Rostered down from 18 to 12 dirs, 2026-09-21 (fk#1195, "keep members under 10, add tasks to
existing members, never create new ones").** jefe, dumbledore, roomba, custodian, signals, and
datta were archived; their live duties moved onto the members below (marie's Part E/F, nerd's
datadog lane, gru's own step 9) rather than staying separate dispatch targets. vp and
librarian-scrub stay separate dispatch targets despite the letter of the PRD's 10-dir budget —
both hit a real tool-grant/cadence conflict with the member they'd otherwise fold into (see
judge-judy.md and librarian.md for the specifics); that is a stated deviation, not an oversight.

| member | cadence (UTC) | job |
|---|---|---|
| gru | hourly :03 | orchestrator: reads runway, picks how many minions, fans out; also computes lane coverage and spawns nerd on demand (folded from datta), and answers decision/infra asks within the hour |
| minion | spawned by gru | builds a batch of backlog items → `member/…` branch → PR |
| judge-judy | every 15 min | text-only merge-blocking code review of open PRs |
| the-fixer | hourly :47 + webhook on red CI | incident response: red CI/deploy, dark prod, stuck PR, red lint, alert-issue dedup |
| marie | every 4h :33 | backlog hygiene, RICE ranking, stale-claim clearing; also worktree/branch sweep (folded from roomba) and surface-debt hygiene (folded from custodian) |
| sentry | every 3h :17 | uses the product like a person; files what breaks; also re-checks the last deploy live (gate 3) at the top of every pass |
| vp | on `quality:*` labels (`vp_due.sh`) | acceptance judge (kept as its own dispatch target — see fk#1195 note above) |
| nerd | spawned by gru with `lane=<name>` | filed-findings pass (ui/quality/datadog/…); the datadog lane also carries the product funnel read and "doubt the number" rule (folded from signals) |
| red | every 6h :23 | adversary: `members/red/attacks.yaml` |
| dont-shoot-the-messenger | daily 06:30 CT (+12:30, 17:30) | the one voice to the human: the brief |
| librarian | daily 05:15 | tends memory dirs; writes INTENT.md |
| librarian-scrub | hourly :06 | the shell credential scrub (kept as its own dispatch target — see fk#1195 note above) |

`entrypoint.sh` renders these into `/etc/cron.d/fleet-kit` at container start; the manifest's
`cadence` is documentation of that line, not the source of it. `FLEET_CRON_MEMBERS` in
fleet.env narrows the set. One invalid crontab field silently discards the whole file
(fleet-kit#418): `python3 scripts/validate_crontab.py /etc/cron.d/fleet-kit` inside the container.

**Change a member's instructions:** edit `members/<name>/<name>.md`, PR, `gh pr merge N --auto --squash`
(no merge queue any more -- fleet-kit#1193; never `--admin`), wait for `deploy OK`, then §4. Per-member
caps live in the manifest (`llm.max_turns`, `mandate.limits.max_budget_usd`); the fleet.env
`FLEET_MAX_BUDGET_USD` / `FLEET_BUILDER_MAX_TURNS` reach only `worktree_builder.sh` and judge-judy.

## 6. Accounts and budget

Claude logins on the box: `~/.claude-philanthropy`, `~/.claude-tgp`, `~/.claude-gmail`,
`~/.claude-reif-google` (each a full Claude Code auth dir; `scripts/set_account_token.sh` and
`scripts/verify_account_login.sh` manage them). The pool order and gating come from
`scripts/account_pool.sh`, which reads each account's Maxx meter:

- Maxx server = podman container `maxx` on dino; owner keys in its volume `/data/_auth.json`.
- The fleet's copies: `~/.config/fleet-kit/secrets.env` → `FLEET_MAXX_HANDLE_<ACCT>` /
  `FLEET_MAXX_KEY_<ACCT>` (plus `GH_TOKEN`, `CLOUDFLARE_API_TOKEN`, `NTFY_TOPIC`). Names only
  here, never values. **A Maxx container restart can rewrite a key** (2026-09-18); the fleet then
  reads `maxx_auth_rejected` and shows the account as `calibrating`. Re-sync from `_auth.json`.
- Check one account: `FLEET_MAXX_HANDLE=<h> FLEET_MAXX_KEY=<k> python3 scripts/maxx_reader.py` → `label: ok`.
- Weekly caps reset per account (philanthropy Sun 07:00Z as of 09-2026; the others on their own
  day). `logs/account-pool-exhausted.state` lists who is gated and until when; `fleet_status.sh`
  prints it.
- Ceiling = `FLEET_SHARE_FRACTION` × sustainable pace per hour (fk#1169). Below 0.01 every
  member logs `PACED` and holds. That is budget, not breakage, unless the accounts are healthy,
  in which case it is a ceiling bug (see learnings #23).

## 7. Where things are

| what | path |
|---|---|
| live dials | `~/fleet-kit/instances/nonprofit-atlas/fleet.env` (bash-sourced, last assignment wins; `scripts/fleet_env_lint.py` names duplicates) |
| secrets | `~/.config/fleet-kit/secrets.env` (never in the repo) |
| logs (host view of the container's `/var/log/fleet-kit`) | `~/fleet-kit/instances/nonprofit-atlas/logs/` — `runs.jsonl` (every pass), `<member>.log`, `gitpull.log` (cron canary), `cron_watchdog.log`, `account-pool.log`, `inbox.log`, `fleet.db` |
| deploy log | `~/fleet-kit-logs/auto_deploy.log` |
| host crons | `crontab -l` on dino: auto_deploy every 5 min, liveness/health checks every 5 min, control_plane tick |
| product repo inside the container | `/repo` (bind mount of `instances/nonprofit-atlas/repo`) |

Read logs from the host paths. `podman exec … cat` truncates piped output (391 of 20k lines
on 2026-09-19) and makes a live fleet look weeks dead.

## 8. Tests

`python3 scripts/selftest.py` (~150 s on a Mac, 13 known bash-3.2 failures: diff the FAIL set
against a clean origin/main run, never read the count). CI runs the explicit test-file list in
`.github/workflows/ci.yml`; a new `scripts/test_*.py` never runs until it is added there (fk#1156).
