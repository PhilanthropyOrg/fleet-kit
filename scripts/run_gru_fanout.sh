#!/bin/bash
# run_gru_fanout.sh — cron's entry point for gru.
#
# HISTORY: this script used to compute N (via fanout.py's Fibonacci ladder over headroom) and
# spawn N independent gru instances, each racing to claim its own item -- see git history for
# that version if you need it. Reif, 2026-08-21: gru itself should decide runway/priority/N,
# not have N handed to it by a bash script it can't see the reasoning of (docs/gru-minions.md
# is the full PRD). gru is the orchestrator now -- it reads maxx/fleet_db itself (gru.md steps
# 1-3), claims its own chosen items, and spawns MINION instances itself via `--item <n>` (see
# run_member.sh's --item flag). This script's only remaining job is being cron's one call.
set -uo pipefail

# set -a/+a: a plain . only sets local shell vars, invisible to claude -p (a separate
# exec) -- see run_member.sh for the full incident writeup (2026-08-22 fleet-wide auth outage).
[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-./fleet.env}"; set +a; }
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# gh#9840: gru showed 38 starts in 24h against 24 cron slots, plus 20 dispatch_skipped rows per 6h.
# Two extra launchers: finish_deploy's post-cutover kick (scripts/deploy.sh, gh#622) and a second
# pass that only lost run_member.sh's gh#3220 flock AFTER the sweeps below ran and logged a skip
# row. Exit here, before any sweep or run row, when (a) a gru pass already holds that same lock, or
# (b) this is the deploy kick and gru started less than FLEET_GRU_KICK_MIN_GAP_MIN (45) min ago.
GRU_LOCK_DIR="${TMPDIR:-/tmp}/fleet-kit-member-locks"
GRU_START_STAMP="$GRU_LOCK_DIR/gru.last-start"
mkdir -p "$GRU_LOCK_DIR" 2>/dev/null || true
if [ "$#" -eq 0 ]; then
  if ! ( exec 9>"$GRU_LOCK_DIR/gru.lock"; flock -n 9 ); then
    echo "[run_gru_fanout] a gru pass is already running -- not starting another (gh#9840)"
    exit 0
  fi
  if [ "${FLEET_FIRED_BY:-}" = "deploy_kick" ] && [ -f "$GRU_START_STAMP" ]; then
    gap_s=$(( $(date +%s) - $(stat -c %Y "$GRU_START_STAMP" 2>/dev/null || echo 0) ))
    if [ "$gap_s" -lt $(( ${FLEET_GRU_KICK_MIN_GAP_MIN:-45} * 60 )) ]; then
      echo "[run_gru_fanout] deploy kick skipped: gru started ${gap_s}s ago (gh#9840)"
      exit 0
    fi
  fi
  touch "$GRU_START_STAMP" 2>/dev/null || true
  # dumbledore msg#1091: the check above lets go of gru.lock at once, and run_member.sh only
  # takes it after the sweeps -- so 4 launches in 32s (2026-10-10 03:46Z) each ran the intake
  # below at the same time, and each posted the same needs-spec comment on #11987 before any
  # of the others' landed. Hold a sweeps lock until the exec; a launch that loses it exits.
  exec 8>"$GRU_LOCK_DIR/gru-sweeps.lock"
  if ! flock -n 8; then
    echo "[run_gru_fanout] another gru launch is running the sweeps -- not starting another (msg#1091)"
    exit 0
  fi
fi

# Claims are leases (2026-09-26). Before gru's model starts, release every fleet:claimed item
# nobody is working (no live runner, no PR/branch activity for FLEET_CLAIM_LEASE_MIN=60 min).
# 12 hours of gru passes skipped 8 Reif-priority items a dead minion batch still "held", until
# a human removed the label. Deterministic, fail-open (a failed sweep never blocks the pass);
# gru step 0 reads the result with `stale_claims.py last`. Runs from the product checkout so gh
# resolves the repo from its remote, the same way gru's own gh calls do.
# gh#8197: first, give every run that died without an ending (SIGKILL, OOM, container stop) its
# terminal row, so no run stays `started` and the claim sweep below reads the truth.
timeout 60 python3 "$KIT_DIR/scripts/open_runs.py" close-lost \
  || echo "[run_gru_fanout] close-lost sweep failed (exit $?) -- continuing without it"
( cd "${FLEET_REPO:-/repo}" 2>/dev/null || true
  timeout 900 python3 "$KIT_DIR/scripts/stale_claims.py" release ) \
  || echo "[run_gru_fanout] stale_claims sweep failed (exit $?) -- continuing without it"
# philanthropy#8218: normalize EVERY open backlog item before gru sees it (quality:solid default,
# needs-spec + comment + one message to marie/jefe), message hq the open needs-prod-access items,
# and message hq the minion drafts that are done and green. Fail-open, like the sweep above.
( cd "${FLEET_REPO:-/repo}" 2>/dev/null || true
  timeout 900 python3 "$KIT_DIR/scripts/gate_drops.py" intake ) \
  || echo "[run_gru_fanout] backlog intake failed (exit $?) -- continuing without it"
( cd "${FLEET_REPO:-/repo}" 2>/dev/null || true
  timeout 120 python3 "$KIT_DIR/scripts/minion_checkpoint.py" complete --notify ) \
  || echo "[run_gru_fanout] merge-ready scan failed (exit $?) -- continuing without it"

exec 8>&-
exec bash "$KIT_DIR/scripts/run_member.sh" gru "$@"
