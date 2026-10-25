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
# The day is the box's local day: gru.log and minion.log are stamped in local time (CDT), so a
# UTC default turned the day at 7 PM Central and every later run greped an empty "tomorrow".
DAY="${1:-$(date +%F)}"

passes() { grep -c "^\[$DAY.*pass end rc=" "$LOG_DIR/$1.log" 2>/dev/null || true; }
fails()  { grep "^\[$DAY.*pass end rc=" "$LOG_DIR/$1.log" 2>/dev/null | grep -vc "rc=0" || true; }
hours()  { grep -oE "^\[$DAY [0-9]{2}" "$LOG_DIR/$1.log" 2>/dev/null | sort -u | wc -l || true; }
n() { [ -n "${1:-}" ] && [ "${1:-}" -eq "${1:-}" ] 2>/dev/null && echo "$1" || echo 0; }

# Minions mostly run on the worker node (lucky) now, and its minion.log stays there. Only dino's
# own passes reach $LOG_DIR/minion.log, so 10-24 read 2 passes / 17 idle hours while lucky ran 7.
# node_minion.sh relays every node run row into this box's runs.jsonl, beside the local runs'
# rows, so count minion passes from there: a pass is a finished row (not started, and not a run
# that was declined before it began), its hours are every local hour from start to end (or to now, while it runs).
# Prints "<passes> <hours> <failed>", or nothing if runs.jsonl is missing or unreadable.
runs_counts() {
  [ -r "$LOG_DIR/runs.jsonl" ] || return 0
  python3 - "$LOG_DIR/runs.jsonl" "$DAY" <<'PY' 2>/dev/null || true
import datetime as dt, json, sys
RAN = {"ok", "quiet", "reported_nothing", "killed", "timed_out", "report_lost"}
FAILED = {"killed", "timed_out", "report_lost"}
day = dt.date.fromisoformat(sys.argv[2])
starts, ends, cap, done = {}, [], {}, set()
with open(sys.argv[1], errors="replace") as f:
    for line in f:
        if '"minion"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("member") != "minion" or not isinstance(r.get("ts"), (int, float)):
            continue
        if r.get("status") == "started":
            starts[r.get("run_id")] = r["ts"]
            cap[r.get("run_id")] = r["ts"] + float(r.get("timeout_s") or 6600)
        else:
            done.add(r.get("run_id"))
            if r.get("status") in RAN:
                ends.append(r)
passes = failed = 0
hours = set()
for r in ends:
    end = dt.datetime.fromtimestamp(r["ts"])
    start = dt.datetime.fromtimestamp(min(starts.get(r.get("run_id"), r["ts"]), r["ts"]))
    if end.date() == day:
        passes += 1
        failed += r["status"] in FAILED or (r.get("exit_code") or 0) != 0
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        if t.date() == day:
            hours.add(t.hour)
        t += dt.timedelta(hours=1)
# A pass still running is building in its hours too (up to now, or its timeout if it went quiet).
now = dt.datetime.now().timestamp()
for rid, ts in starts.items():
    if rid not in done and ts <= now:
        ends_at = dt.datetime.fromtimestamp(min(now, cap[rid]))
        t = dt.datetime.fromtimestamp(ts).replace(minute=0, second=0, microsecond=0)
        while t <= ends_at:
            if t.date() == day:
                hours.add(t.hour)
            t += dt.timedelta(hours=1)
print(passes, len(hours), failed)
PY
}

DP=$(n "$(passes minion)"); DF=$(n "$(fails minion)"); DH=$(n "$(hours minion)")
GP=$(n "$(passes gru)");    GH=$(n "$(hours gru)")
read -r RP RH RF <<<"$(runs_counts)" || true
if [ -n "${RP:-}" ]; then
  SRC="runs.jsonl"; MP=$(n "$RP"); MH=$(n "$RH"); MF=$(n "$RF")
  # runs.jsonl is the sum; a relay gap must not make the count drop below dino's own log.
  [ "$DP" -gt "$MP" ] && { MP=$DP; MF=$DF; }
  [ "$DH" -gt "$MH" ] && MH=$DH
else
  SRC="minion.log"; MP=$DP; MH=$DH; MF=$DF
fi
NODE=$((MP - DP)); [ "$NODE" -lt 0 ] && NODE=0
IDLE=0; [ "$GH" -gt "$MH" ] && IDLE=$((GH - MH))

LINE=$(printf '{"day":"%s","gru_passes":%s,"gru_hours":%s,"minion_passes":%s,"minion_hours":%s,"minion_failed":%s,"idle_hours_forfeited":%s,"minion_passes_dino":%s,"minion_passes_node":%s,"minion_source":"%s"}' \
  "$DAY" "$GP" "$GH" "$MP" "$MH" "$MF" "$IDLE" "$DP" "$NODE" "$SRC")
echo "$LINE"
{ printf '%s\n' "$LINE" >> "$LOG_DIR/spawn_counts.jsonl"; } 2>/dev/null || true
