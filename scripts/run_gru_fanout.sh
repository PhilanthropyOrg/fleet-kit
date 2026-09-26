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

# Claims are leases (2026-09-26). Before gru's model starts, release every fleet:claimed item
# nobody is working (no live runner, no PR/branch activity for FLEET_CLAIM_LEASE_MIN=60 min).
# 12 hours of gru passes skipped 8 Reif-priority items a dead minion batch still "held", until
# a human removed the label. Deterministic, fail-open (a failed sweep never blocks the pass);
# gru step 0 reads the result with `stale_claims.py last`. Runs from the product checkout so gh
# resolves the repo from its remote, the same way gru's own gh calls do.
( cd "${FLEET_REPO:-/repo}" 2>/dev/null || true
  timeout 300 python3 "$KIT_DIR/scripts/stale_claims.py" release ) \
  || echo "[run_gru_fanout] stale_claims sweep failed (exit $?) -- continuing without it"

exec bash "$KIT_DIR/scripts/run_member.sh" gru "$@"
