#!/usr/bin/env bash
# node_gate.sh -- the ONLY thing the hub's key can run on a worker node (authorized_keys
# `command=`, installed by node_up.sh). It accepts exactly `minion --items <n,..>` and runs that
# batch inside the node's worker container.
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
if ! [[ "$cmd" =~ ^minion\ --items\ ([0-9]{1,7}(,[0-9]{1,7}){0,9})$ ]]; then
  echo "node-refused: only 'minion --items <n,..>' is allowed"
  exit 2
fi
items="${BASH_REMATCH[1]}"

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

runs="$LOG_DIR/runs.jsonl"
mkdir -p "$LOG_DIR" && touch "$runs"
prefix="\"run_id\": \"minion-item${items//,/_}-"
seen=$(wc -l < "$runs")
relay() {  # complete lines only: wc -l never counts a half-written last line
  local total
  total=$(wc -l < "$runs")
  [ "$total" -lt "$seen" ] && seen=0
  [ "$total" -gt "$seen" ] && sed -n "$((seen + 1)),${total}p" "$runs" | grep -F "$prefix" | sed 's/^/node-row /'
  seen=$total
}

echo "node-accepted host=$(hostname) slot=$got/$SLOTS items=$items"
"$PODMAN" exec -e FLEET_RUN_NOW=1 "$CONTAINER" \
  bash /fleet-kit/scripts/run_member.sh minion --items "$items" >/dev/null 2>&1 </dev/null &
pid=$!
while kill -0 "$pid" 2>/dev/null; do
  relay
  sleep "$POLL_S"
done
wait "$pid"; rc=$?
relay
echo "node-done rc=$rc"
exit "$rc"
