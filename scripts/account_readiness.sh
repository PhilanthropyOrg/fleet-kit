#!/bin/bash
# account_readiness.sh -- how many pool accounts are actually usable RIGHT NOW, before
# committing to a claim+spawn cycle that account_pool.sh would otherwise only discover is
# doomed AFTER the minion is already spawned and burning turns.
#
# WHY THIS EXISTS: gru already reads budget PACING (maxx_reader.py, fleet_db.py spend) in its
# charter's step 1, but never checked account AUTH STATE before spawning -- confirmed live,
# 2026-08-25: multiple real gru passes claimed items, spawned minions, and only found out via
# ALL_ACCOUNTS_EXHAUSTED (account_pool.sh exit 3) after the fact, wasting the claim + the
# minion's spin-up turns on work that was never going to run. Gru's own self-critique named
# this exact gap: "no visibility into account_pool.sh's live state before spawning... no
# evidence a pre-check tool exists in my allowlist to have caught this earlier."
#
# WHAT IT CHECKS: account_pool.sh's own exhaustion-gate state file (the ground truth the pool
# itself already maintains, see account_pool.sh's _account_pool_budget_verdict) -- cheap, no
# API call, just a file read. Prints how many of FLEET_ACCOUNTS are NOT currently gated, so
# gru can size N against real spawn capacity instead of budget alone.
#
# Usage: bash account_readiness.sh
# Output (stdout, one line, machine-parseable):
#   "ready=<N> total=<N> ready_names=<name,...> gated=<name:reason,name:reason,...>"
# fk#1148: ready_names is there so the line can be diffed against FLEET_ACCOUNTS by eye, and
# an unset FLEET_ACCOUNTS is a loud exit 2 rather than a confident "ready=1 total=1".
# gh#134: the state file's 3rd column (written by account_pool.sh for the unauthenticated/
# other branches, absent -> "exhausted" for the original 2-column format) is now surfaced here
# too -- a bare "gated=acctname" told a reader an account was down but not why, and "why" is
# exactly what tells a human whether to wait (a weekly reset) or act (re-authenticate).
set -uo pipefail

# fk#1148: FLEET_ACCOUNTS unset used to fall back to the single name "primary", so running
# this without the instance env printed a confident "ready=1 total=1 gated=" for a
# three-account pool. That is indistinguishable from a genuinely healthy one-account pool,
# and it is the FIRST line a human or an agent reads when asking "does the fleet have
# capacity right now" -- during the 2026-09-19 outage it answered "everything is fine" while
# two of three accounts were gated. A capacity meter that can silently report on a subset of
# the pool cannot be used to diagnose a capacity problem, so this now says so loudly.
if [ -z "${FLEET_ACCOUNTS:-}" ]; then
  echo "ready=0 total=0 gated= error=FLEET_ACCOUNTS_unset" >&2
  echo "account_readiness: FLEET_ACCOUNTS is not set -- refusing to guess the pool." >&2
  echo "  Run it with the instance env, e.g.:" >&2
  echo "    set -a; . <instance-dir>/fleet.env; set +a; bash \$0" >&2
  exit 2
fi
ACCOUNTS="$FLEET_ACCOUNTS"
# Default MUST match account_pool.sh's own default exactly (same env var, same fallback) --
# confirmed live, 2026-08-25: this used $FLEET_LOG_DIR:-/var/log/fleet-kit while
# account_pool.sh uses $FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit. Without FLEET_LOG_DIR
# explicitly exported, the two scripts silently read/write DIFFERENT files -- this one reported
# a false ready=2 while the real gate file (account_pool.sh's) showed both accounts gated. gru
# itself caught this via cross-check and filed nonprofit-atlas#3163 rather than trust it blind.
STATE_FILE="${ACCOUNT_POOL_STATE_FILE:-${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/account-pool-exhausted.state}"

now=$(date +%s)
ready=0
total=0
gated_names=()
ready_names=()

for acct in $ACCOUNTS; do
  total=$((total + 1))
  is_gated=0
  reason=""
  if [ -f "$STATE_FILE" ]; then
    line=$(awk -v a="$acct" '$1==a' "$STATE_FILE" | tail -1)
    if [ -n "$line" ]; then
      epoch=$(awk '{print $2}' <<<"$line")
      reason=$(awk '{print $3}' <<<"$line")
      [ -z "$reason" ] && reason="exhausted"
      if [ -n "$epoch" ] && [ "$epoch" -gt "$now" ]; then
        is_gated=1
      fi
    fi
  fi
  if [ "$is_gated" -eq 1 ]; then
    gated_names+=("${acct}:${reason}")
  else
    ready=$((ready + 1))
    ready_names+=("$acct")
  fi
done

gated_str=$(IFS=,; echo "${gated_names[*]:-}")
# fk#1148: name the READY accounts too. Counts alone cannot be sanity-checked against
# FLEET_ACCOUNTS by eye -- "ready=2 total=3" does not say WHICH two, so a reader cannot tell
# a healthy pair from the wrong pair, and cannot spot a pool that lost a member entirely.
ready_str=$(IFS=,; echo "${ready_names[*]:-}")
echo "ready=$ready total=$total ready_names=$ready_str gated=$gated_str"

# Exit 0 if at least one account is ready, 1 if the whole pool is currently gated -- lets a
# caller do `account_readiness.sh || echo "skip this pass"` without parsing the line.
[ "$ready" -gt 0 ]
