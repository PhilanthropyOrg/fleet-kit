#!/usr/bin/env bash
# dispatch_member.sh -- start one minion or nerd pass DETACHED from the caller (gru).
#
#   bash /fleet-kit/scripts/dispatch_member.sh minion --items <n1,n2,n3>     # returns at once
#   bash /fleet-kit/scripts/dispatch_member.sh nerd --task "lane=<lane> ..."  # returns at once
#   bash /fleet-kit/scripts/dispatch_member.sh --wait <pid> [<pid> ...]      # blocks <= 540s
#
# THE GAP (2026-09-26). gru spawned minions with Bash(run_in_background). A background task
# dies with the `claude -p` pass that started it. gru ended its turn at 13:45:21 and minion
# #7938, resuming checkpoint #8134 three minutes in, was SIGTERMed at 13:45:27 (rc=143). Its
# 13:29:40 session took #7937 the same way. Those rc=143s are what benched five reif-priority
# items as dead ends (fk#1313). dispatch_fixer.sh (#1303) fixed the same thing for fixers.
#
# Each pass gets its own session (setsid), stdin closed, output in
# $FLEET_LOG_DIR/<member>-<items|task-hash>.dispatch.log. It prints `pid=<N>`; --wait polls those
# pids for up to FLEET_DISPATCH_WAIT_S (540, under the Bash tool's 600s ceiling), then prints one
# line per pid, `done` or `running`. A pass still running when gru's turn ends keeps running and
# writes its own runs.jsonl record. Nothing here decides WHAT to run: run_member.sh's dispatch
# lock still dedups.
set -uo pipefail
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_MEMBER="${FLEET_RUN_MEMBER:-$KIT_DIR/scripts/run_member.sh}"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
USAGE="usage: dispatch_member.sh minion --items <n,..> | nerd --task \"lane=...\" | --wait <pid> ..."

if [ "${1:-}" = "--wait" ]; then
  shift
  deadline=$(( $(date +%s) + ${FLEET_DISPATCH_WAIT_S:-540} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    alive=0
    for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ "$alive" -eq 0 ] && break
    sleep "${FLEET_DISPATCH_POLL_S:-10}"
  done
  for p in "$@"; do
    if kill -0 "$p" 2>/dev/null; then echo "pid=$p running"; else echo "pid=$p done"; fi
  done
  exit 0
fi

member="${1:-}"
case "$member" in
  minion) [ "${2:-}" = "--items" ] && [ -n "${3:-}" ] || { echo "$USAGE" >&2; exit 2; }
          tag="items${3//,/_}" ;;
  nerd)   [ "${2:-}" = "--task" ] && [ -n "${3:-}" ] || { echo "$USAGE" >&2; exit 2; }
          tag="$(printf '%s' "$3" | sed -n 's/^lane=\([A-Za-z0-9_-]*\).*/\1/p')"; tag="lane-${tag:-x}" ;;
  *)      echo "$USAGE" >&2; exit 2 ;;
esac
log="$LOG_DIR/$member-$tag.dispatch.log"
FLEET_RUN_NOW=1 setsid nohup bash "$RUN_MEMBER" "$@" >>"$log" 2>&1 </dev/null &
echo "dispatched $member ${3:-} (detached, pid=$!, log $log)"
