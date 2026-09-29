#!/usr/bin/env bash
# dispatch_fixer.sh -- start one the-fixer --item pass per PR, DETACHED from the caller.
#
#   bash /fleet-kit/scripts/dispatch_fixer.sh <pr> [<pr> ...]      # returns at once
#
# THE GAP (2026-09-25, 20:26 UTC). gru's step 0 spawned fixers for six red PRs with
# Bash(run_in_background). gru ended its turn at 20:25:57; at 20:26:06 every fixer still
# running was killed (exit 143) -- #7982's with its fix half done -- because a background
# task dies with the `claude -p` pass that started it. the-fixer's own hourly fan-out lost
# three sub-passes the same way at 20:26:20. A fix cycle (fix, verified_test.sh, push, wait for
# CI) outlives any sensible dispatcher turn, so a fixer must not share its dispatcher's fate.
#
# Each PR gets its own session (setsid) with stdin closed and output in
# $FLEET_LOG_DIR/the-fixer-item<N>.dispatch.log. Nothing here decides WHETHER to fix: the
# dispatch lock and red_prs.py claim inside run_member.sh still dedup, so calling this twice for
# one PR costs one no-op pass, never two fixers.
set -uo pipefail
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_MEMBER="${FLEET_RUN_MEMBER:-$KIT_DIR/scripts/run_member.sh}"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# THE CAP (2026-09-29, dumbledore). Nothing bounded how many fixers ran at once: 18 sub-passes
# started in one hour on 09-28 21h and 15 at 09-29 05h, on a 7-core box that runs 3 test slots.
# Each one queued on verified_test.sh behind the others and died at its 3600s ceiling: on 09-29,
# 25 timed out and 8 finished ok. A PR over the cap is not lost -- it stays red, and the next
# hourly check.sh lists it again -- so it waits its turn instead of starving the ones running.
MAX="${FLEET_FIXER_ITEM_MAX:-3}"
live_items() {
  pgrep -af "run_member.sh the-fixer --item [0-9]" 2>/dev/null \
    | sed -n 's/.*the-fixer --item \([0-9][0-9]*\).*/\1/p' | sort -u | wc -l
}
LIVE="$(live_items)"

[ $# -gt 0 ] || { echo "usage: dispatch_fixer.sh <pr> [<pr> ...]" >&2; exit 2; }
for pr in "$@"; do
  pr="${pr#\#}"
  # `kit:N` is fleet-kit's own PR N (red_prs.py due --kit): the fixer works it in a kit clone.
  KIT=""; case "$pr" in kit:*) KIT=1; pr="${pr#kit:}" ;; esac
  case "$pr" in (""|*[!0-9]*) echo "dispatch_fixer: skipping '$pr' (not a PR number)" >&2; continue ;; esac
  if [ "$LIVE" -ge "$MAX" ]; then
    echo "deferred the-fixer --item $pr ($LIVE fixers already running, cap FLEET_FIXER_ITEM_MAX=$MAX; next hourly pass picks it up)"
    continue
  fi
  LIVE=$((LIVE + 1))
  FLEET_FIXER_KIT="$KIT" setsid nohup bash "$RUN_MEMBER" the-fixer --item "$pr" \
    >>"$LOG_DIR/the-fixer-item${KIT:+kit}$pr.dispatch.log" 2>&1 </dev/null &
  echo "dispatched the-fixer --item ${KIT:+kit:}$pr (detached, pid $!, log $LOG_DIR/the-fixer-item${KIT:+kit}$pr.dispatch.log)"
done
