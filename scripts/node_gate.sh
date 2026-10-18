#!/usr/bin/env bash
# node_gate.sh -- the ONLY thing the hub's key can run on a worker node (authorized_keys
# `command=`, installed by node_up.sh). It accepts exactly `minion --items <n,..>` and runs that
# batch inside the node's worker container, or `attach --items <n,..>` to follow one already running.
#
# WHY A WORKER NODE, NOT A SECOND FLEET (2026-09-29). dino ran at load 12-21 on 7 cores while
# lucky sat idle. A second full instance would run a second gru against the same backlog, and a
# claim is two non-atomic `gh` calls (board_github.py's header: "NOT atomic -- GitHub has no
# test-and-set"). So the hub's gru still plans, claims and dispatches every item, alone; this
# node only adds CPU. Nothing here can pick, claim or file anything.
#
# Protocol on stdout (read by node_minion.sh on the hub):
#   node-refused: <why>       nothing ran; the hub runs the batch itself
#   node-accepted host=<h>    the batch is running here; the hub must NOT run it too
#   node-row <json>           a runs.jsonl row for this batch, relayed as it lands
#   node-done rc=<n>          run_member.sh's exit code
#   node-gone                 (attach only) no run of this batch is live and none recorded an end
set -uo pipefail
trap '' PIPE  # the hub hanging up must not kill a running minion
[ -f "${FLEET_NODE_ENV:-$HOME/.config/fleet-kit/node.env}" ] && . "${FLEET_NODE_ENV:-$HOME/.config/fleet-kit/node.env}"
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${FLEET_NODE_CONTAINER:-fleet-worker}"
PODMAN="${FLEET_NODE_PODMAN:-podman}"
LOG_DIR="${FLEET_NODE_LOG_DIR:-$KIT_DIR/instances/${FLEET_NODE_INSTANCE:-default}/logs}"
SLOTS="${FLEET_NODE_SLOTS:-3}"
SLOT_DIR="${FLEET_NODE_SLOT_DIR:-$HOME/.cache/fleet-node-slots}"
POLL_S="${FLEET_NODE_POLL_S:-5}"

cmd="${SSH_ORIGINAL_COMMAND:-}"
if ! [[ "$cmd" =~ ^(minion|attach)\ --items\ ([0-9]{1,7}(,[0-9]{1,7}){0,9})$ ]]; then
  echo "node-refused: only 'minion --items <n,..>' or 'attach --items <n,..>' is allowed"
  exit 2
fi
verb="${BASH_REMATCH[1]}"
items="${BASH_REMATCH[2]}"

runs="$LOG_DIR/runs.jsonl"
prefix="\"run_id\": \"minion-item${items//,/_}-"
relay() {  # complete lines only: wc -l never counts a half-written last line
  local total
  total=$(wc -l < "$runs")
  [ "$total" -lt "$seen" ] && seen=0
  [ "$total" -gt "$seen" ] && sed -n "$((seen + 1)),${total}p" "$runs" | grep -F "$prefix" | sed 's/^/node-row /'
  seen=$total
}
# One state file per batch: `start=<runs.jsonl line>` when it starts, `rc=<n>` when it ends. The
# runner holds a lock on it for its whole life, so attach can tell "still running" from "gone".
state="$SLOT_DIR/batch-${items//,/_}"

if [ "$verb" = attach ]; then  # fk#1670: the hub's link dropped mid-run; replay and follow
  start=$(sed -n 's/^start=//p' "$state" 2>/dev/null)
  [ -n "$start" ] || { echo "node-gone"; exit 5; }
  seen="$start"
  while :; do
    relay
    end=$(sed -n 's/^rc=//p' "$state")
    [ -n "$end" ] && { relay; echo "node-done rc=$end"; exit "$end"; }
    if flock -n "$state" true; then  # nobody holds it: the runner is gone; re-read, it may just have ended
      end=$(sed -n 's/^rc=//p' "$state")
      [ -n "$end" ] && { relay; echo "node-done rc=$end"; exit "$end"; }
      echo "node-gone"; exit 5
    fi
    sleep "$POLL_S"
  done
fi

mkdir -p "$SLOT_DIR"
got=""
for i in $(seq 1 "$SLOTS"); do
  exec 8>"$SLOT_DIR/slot$i"
  if flock -n 8; then got="$i"; break; fi
  exec 8>&-
done
[ -n "$got" ] || { echo "node-refused: busy ($SLOTS/$SLOTS slots)"; exit 3; }

if [ "$("$PODMAN" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
  echo "node-refused: container $CONTAINER is not running"
  exit 4
fi

mkdir -p "$LOG_DIR" && touch "$runs"
seen=$(wc -l < "$runs")
exec 9>>"$state"
flock -n 9 || { echo "node-refused: batch $items is already running here"; exit 3; }
: > "$state" && echo "start=$seen" >&9

# The hub's accounts (node_minion.sh sends `acct <name> <token>` lines on stdin, never argv): each
# goes in as FLEET_NODE_TOKEN_<ACCT> and FLEET_NODE_ACCOUNTS keeps the hub's order; podman gets
# the variable NAMES only, so no token is in any process's argv. run_member.sh prefers them over
# the node's own fleet.env accounts. An old hub sends nothing: the node's own accounts, as before.
EXTRA=()
accts=""
# The hub also sends `env FLEET_ACTIONS_BUDGET_USD <number>` (node_minion.sh): only that name,
# only a number, so the push gate here reads the hub's budget, not a stale copy's.
while IFS=' ' read -r -t "${FLEET_NODE_STDIN_WAIT_S:-10}" kw name tok; do
  if [ "$kw" = "env" ] && [ "$name" = "FLEET_ACTIONS_BUDGET_USD" ] && [[ "$tok" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    export "$name=$tok"; EXTRA+=(-e "$name"); continue
  fi
  [ "$kw" = "acct" ] && [[ "$name" =~ ^[a-z0-9-]{1,40}$ ]] && [[ "$tok" =~ ^sk-ant-[A-Za-z0-9_-]+$ ]] || continue
  var="FLEET_NODE_TOKEN_$(echo "$name" | tr '[:lower:]-' '[:upper:]_')"
  export "$var=$tok"
  EXTRA+=(-e "$var")
  accts="${accts:+$accts }$name"
done
if [ -n "$accts" ]; then
  export FLEET_NODE_ACCOUNTS="$accts"
  EXTRA+=(-e FLEET_NODE_ACCOUNTS)
fi
echo "node-accepted host=$(hostname) slot=$got/$SLOTS items=$items${accts:+ accounts=${accts// /,}}"
"$PODMAN" exec -e FLEET_RUN_NOW=1 ${EXTRA[@]+"${EXTRA[@]}"} "$CONTAINER" \
  bash /fleet-kit/scripts/run_member.sh minion --items "$items" >/dev/null 2>&1 </dev/null 9>&- &
pid=$!
while kill -0 "$pid" 2>/dev/null; do
  relay
  sleep "$POLL_S"
done
wait "$pid"; rc=$?
relay
echo "rc=$rc" >&9
echo "node-done rc=$rc"
exit "$rc"
