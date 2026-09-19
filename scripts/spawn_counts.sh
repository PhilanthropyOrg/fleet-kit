#!/bin/bash
# spawn_counts.sh -- how many builds the fleet actually ran today, as one JSON line.
#
# WHY: on 2026-09-18 the question "is the fleet building?" took an 83MB grep of minion.log to
# answer, and the first three answers were wrong. Nothing counted passes, so an idle hour --
# which is FORFEITED, since fanout's allowance does not roll over ("underspending is exactly
# as wrong as overspending", fanout.py) -- was invisible. gru fired 20 hours that day and
# minion built in only 15 of them; five hours were silently lost.
#
# Emits one JSON line to stdout AND appends to $FLEET_LOG_DIR/spawn_counts.jsonl for a trend.
set -euo pipefail
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
DAY="${1:-$(date -u +%F)}"

passes() { grep -c "^\[$DAY.*pass end rc=" "$LOG_DIR/$1.log" 2>/dev/null || true; }
fails()  { grep "^\[$DAY.*pass end rc=" "$LOG_DIR/$1.log" 2>/dev/null | grep -vc "rc=0" || true; }
hours()  { grep -oE "^\[$DAY [0-9]{2}" "$LOG_DIR/$1.log" 2>/dev/null | sort -u | wc -l || true; }
n() { [ -n "${1:-}" ] && [ "${1:-}" -eq "${1:-}" ] 2>/dev/null && echo "$1" || echo 0; }

MP=$(n "$(passes minion)"); MF=$(n "$(fails minion)"); MH=$(n "$(hours minion)")
GP=$(n "$(passes gru)");    GH=$(n "$(hours gru)")
IDLE=0; [ "$GH" -gt "$MH" ] && IDLE=$((GH - MH))

LINE=$(printf '{"day":"%s","gru_passes":%s,"gru_hours":%s,"minion_passes":%s,"minion_hours":%s,"minion_failed":%s,"idle_hours_forfeited":%s}' \
  "$DAY" "$GP" "$GH" "$MP" "$MH" "$MF" "$IDLE")
echo "$LINE"
printf '%s\n' "$LINE" >> "$LOG_DIR/spawn_counts.jsonl" 2>/dev/null || true
