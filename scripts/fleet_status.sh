#!/bin/bash
# fleet_status.sh -- every layer's verdict on one screen. Run ON THE BOX (needs podman).
#
# WHY (fk#1171, 2026-09-19): the fleet has six self-protecting layers (container, deploy,
# cron, account pool, ceiling/pacing, dispatch lock) and each reports in its own word --
# PACED, gated, calibrating, dispatch_skipped, quiet, stale. Cron was dead for 2h while
# liveness said OK and every other layer said "waiting". Nobody could tell "choosing not
# to work" from "broken" without reading five logs. This prints all of them, in order,
# with a DEAD / WAIT / OK verdict per layer. Read top to bottom; the first DEAD is the bug.
#
#   bash scripts/fleet_status.sh                # instance defaults below
#   FLEET_CONTAINER_NAME=x KIT_DIR=~/fleet-kit bash scripts/fleet_status.sh
set -u
C="${FLEET_CONTAINER_NAME:-philanthropy}"
KIT="${KIT_DIR:-$HOME/fleet-kit}"
DEPLOY_LOG="${FLEET_DEPLOY_LOG:-$HOME/fleet-kit-logs/auto_deploy.log}"
# Logs are read from the HOST bind mount, never via `podman exec cat`: podman truncates a
# piped exec's stdout (391 of 20,230 lines came through on 2026-09-19), which made runs.jsonl
# look 28 days stale.
LOGS="${FLEET_LOG_DIR:-$HOME/fleet-kit/instances/nonprofit-atlas/logs}"
now=$(date +%s)
pe() { podman exec "$C" sh -c "$1" 2>/dev/null; }
row() { printf '%-5s %-10s %s\n' "$1" "$2" "$3"; }

# 1. container
if podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$C"; then
  row OK container "$C up since $(podman inspect "$C" --format '{{.State.StartedAt}}' | cut -c1-19)"
else
  row DEAD container "$C not running: bash $KIT/scripts/refresh_container.sh $C"
  exit 1
fi

# 2. deploy: what runs vs what main says
live=$(grep -h 'deploy OK at' "$DEPLOY_LOG" 2>/dev/null | tail -1 | sed 's/.*deploy OK at //' | cut -c1-7)
main=$(git -C "$KIT" ls-remote -q origin main 2>/dev/null | cut -c1-7)
when=$(grep -h 'deploy OK at' "$DEPLOY_LOG" 2>/dev/null | tail -1 | cut -c2-17)
if [ -n "$live" ] && [ "$live" = "$main" ]; then row OK deploy "container on $live = origin/main ($when)"
elif [ -n "$live" ]; then row WAIT deploy "container on $live, main at $main; auto_deploy coalesces 2h, last $when"
else row DEAD deploy "no 'deploy OK' in $DEPLOY_LOG"; fi

# 3. cron canary (the only proof the scheduler fires)
last=$(grep -ahE "^[A-Z][a-z]{2} [A-Z][a-z]{2} +[0-9]+ [0-9:]+ UTC [0-9]{4}$" "$LOGS/gitpull.log" | tail -1)
if [ -n "$last" ]; then
  age=$(( now - $(date -u -d "$last" +%s 2>/dev/null || echo 0) ))
  if [ "$age" -lt 1200 ]; then row OK cron "last tick ${age}s ago"
  else row DEAD cron "no tick for ${age}s (>20min). Process restart does NOT fix this (fk#1171): bash $KIT/scripts/refresh_container.sh $C"; fi
else row DEAD cron "gitpull.log has no date line at all"; fi

# 4. kill switch
en=$(pe 'grep -h "^FLEET_ENABLED=" /fleet-kit/fleet.env | tail -1 | cut -d= -f2')
[ "${en:-true}" = "true" ] && row OK enabled "FLEET_ENABLED=true" || row WAIT enabled "FLEET_ENABLED=$en (fleet.env) -- off on purpose?"

# 5. accounts
pool=$(grep -ah "order by week bank" "$LOGS/account-pool.log" | tail -1 | sed "s/.*order by week bank: //")
gated=$(cat "$LOGS/account-pool-exhausted.state" 2>/dev/null | awk -v n="$now" '$2>n {printf "%s(until %s) ", $1, strftime("%m-%d %H:%MZ",$2,1)}')
row "$([ -n "$pool" ] && echo OK || echo DEAD)" accounts "${pool:-no account_pool line} ${gated:+| GATED: $gated}"

# 6. ceiling / pacing (last reading by any member)
cl=$(grep -ah "FLEET_SHARE_CEILING_PCT=" "$LOGS"/*.log | sort | tail -1)
ceil=$(echo "$cl" | cut -c2-17)
cv=$(echo "$cl" | sed "s/.*CEILING_PCT=\([0-9.]*\).*/\1/")
paced=$(grep -ah "PACED:" "$LOGS"/*.log | sort | tail -1 | cut -c2-17)
if [ -z "$cv" ]; then row DEAD ceiling "no reading"
elif awk -v v="$cv" 'BEGIN{exit !(v<0.01)}'; then row WAIT ceiling "$cv at $ceil -- members hold PACED (last $paced). Budget or fk#1169-class bug"
else row OK ceiling "$cv %/hour at $ceil"; fi

# 7. work: last completed llm run per member (runs.jsonl)
RUNS_PY=$(mktemp); cat > "$RUNS_PY" <<'PY'
import sys,json,time
now=time.time(); last={}
for l in sys.stdin:
    try: j=json.loads(l)
    except Exception: continue
    if j.get("status") in ("started","heartbeat"): continue
    last[j["member"]]=j
if not last:
    print("DEAD  runs       runs.jsonl empty"); sys.exit()
rows=sorted(last.values(), key=lambda j:-j["ts"])
newest=now-rows[0]["ts"]
v="OK" if newest<5400 else "DEAD"
print(f"{v:<5} runs       newest finished pass {int(newest/60)}m ago")
for j in rows[:8]:
    print(f"      {j['member']:<12} {int((now-j['ts'])/60):>5}m  {str(j.get('status','')):<16} {(j.get('outcome') or '')[:70]}")
PY
tail -n 4000 "$LOGS/runs.jsonl" | tr -d '\000' | python3 "$RUNS_PY"; rm -f "$RUNS_PY"

# 8. in-flight + output
pe 'ps -eo etime,args | grep "bash /fleet-kit/scripts/run_member.sh" | grep -v "defunct\|grep"' | sed -E 's/.*run_member.sh ([a-z-]+).*/\1/' | sort -u | tr '\n' ' ' | awk '{print "      running:", $0}'
prs=$(pe 'export GH_TOKEN=$(cat /root/.gh_token); cd /repo && gh pr list --state open --json headRefName -q "[.[] | select(.headRefName|startswith(\"member/\"))] | length"')
row OK output "${prs:-?} open member/ PRs on the product repo"
