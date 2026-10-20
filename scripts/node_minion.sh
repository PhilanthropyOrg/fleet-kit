#!/usr/bin/env bash
# node_minion.sh -- run one ALREADY-CLAIMED minion batch on the worker node, or here.
#
#   bash node_minion.sh minion --items <n1,n2,n3>
#
# dispatch_member.sh calls this instead of run_member.sh when a node is configured
# ($FLEET_NODE_DIR/config, an ssh config with a `Host fleet-node` entry, written by node_up.sh).
# gru has already claimed the items; the node's node_gate.sh can only run them.
#
# The node either says `node-accepted` (it owns the batch now) or it doesn't (refused, busy,
# unreachable). Only in the second case does this box run the batch itself, so a batch runs in
# exactly one place. Its runs.jsonl rows are relayed into this box's runs.jsonl as they land, so
# gru's step 7, open_runs.py and the dashboard read a node run like a local one. This process
# lives as long as the remote run, so dispatch_member.sh --wait and stale_claims.py's live-runner
# check (it names --items in its argv) both work unchanged.
set -uo pipefail
shopt -s lastpipe
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_MEMBER="${FLEET_RUN_MEMBER_LOCAL:-$KIT_DIR/scripts/run_member.sh}"
NODE_DIR="${FLEET_NODE_DIR:-/root/.fleet-node}"
SSH="${FLEET_NODE_SSH_BIN:-ssh}"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"

[ "${1:-}" = "minion" ] && [ "${2:-}" = "--items" ] && [[ "${3:-}" =~ ^[0-9]+(,[0-9]+)*$ ]] \
  || { echo "usage: node_minion.sh minion --items <n,..>" >&2; exit 2; }
items="$3"

run_here() {
  echo "[node_minion] $1 -- running items $items on this box"
  rm -f "${seen_rows:-}"
  exec bash "$RUN_MEMBER" minion --items "$items"
}
[ -f "$NODE_DIR/config" ] || run_here "no worker node configured"

# The node spends this box's accounts (Reif, 2026-09-29: "lucky can use all accounts, just has
# to come from dino first"). Per batch, each FLEET_ACCOUNTS account, in order, goes on ssh's
# stdin as `acct <name> <token>` -- never argv. A long-lived CLAUDE_CODE_OAUTH_TOKEN_<ACCT> goes
# as is; a refreshing login sends only its current access token, and only with at least
# FLEET_NODE_TOKEN_MIN_LEFT_MIN left (a batch is <= 90 min). Copying .credentials.json instead
# would log this box out: the refresh token rotates on use. Nothing sent: the node uses its own.
node_tokens() {
  local acct var
  for acct in ${FLEET_ACCOUNTS:-}; do
    [[ "$acct" =~ ^[a-z0-9-]+$ ]] || continue
    var="CLAUDE_CODE_OAUTH_TOKEN_$(echo "$acct" | tr '[:lower:]-' '[:upper:]_')"
    if [ -n "${!var:-}" ]; then printf 'acct %s %s\n' "$acct" "${!var}"; continue; fi
    python3 - "$HOME/.claude-$acct/.credentials.json" "$acct" "${FLEET_NODE_TOKEN_MIN_LEFT_MIN:-120}" <<'PY' 2>/dev/null
import json, sys, time
o = json.load(open(sys.argv[1])).get("claudeAiOauth") or {}
t, left = o.get("accessToken", ""), (o.get("expiresAt", 0) / 1000 - time.time()) / 60
if t.startswith("sk-ant-") and left >= float(sys.argv[3]):
    print(f"acct {sys.argv[2]} {t}")
PY
  done
}

# A post-accept ssh drop is the tunnel (cloudflared), not the run: node_gate.sh traps PIPE and the
# batch keeps going. Ending here left the hub row `started` until open_runs.py called it killed,
# and gru sent the item again (fk#1670: 4 of 8 minion batches on 10-14, #12104 sent 4 times).
# So re-attach: the node replays the batch's rows and follows it to node-done. A row already
# relayed is skipped, so runs.jsonl gets each one once.
accepted=0 done=0 rc=0
seen_rows=$(mktemp) && trap 'rm -f "$seen_rows"' EXIT
talk() {  # talk <gate command> [stdin producer]
  "${2:-true}" | "$SSH" -F "$NODE_DIR/config" -o BatchMode=yes -o ConnectTimeout=30 fleet-node \
      "$1" 2>&1 | while IFS= read -r line; do
    case "$line" in
      node-accepted*) accepted=1; echo "[node_minion] $line" ;;
      "node-row "*)   row="${line#node-row }"
                      grep -Fxq -- "$row" "$seen_rows" && continue
                      printf '%s\n' "$row" >> "$seen_rows"
                      printf '%s\n' "$row" >> "$LOG_DIR/runs.jsonl" ;;
      "node-done "*)  done=1; echo "[node_minion] $line" ;;
      *)              echo "[node_minion] $line" ;;
    esac
  done
  rc=${PIPESTATUS[1]}
}
# The hub's Actions budget goes too (`env <NAME> <number>`): the node's fleet.env is a copy from
# node_up.sh day, and without FLEET_ACTIONS_BUDGET_USD its push gate fell back to the org's
# smaller budget and refused every minion push at 157-193% while the hub read 78% (10-18).
node_stdin() {
  local b="${FLEET_ACTIONS_BUDGET_USD:-}"
  [ -n "$b" ] || b=$(sed -n 's/^FLEET_ACTIONS_BUDGET_USD=//p' "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}" 2>/dev/null | tail -1)
  node_tokens
  [[ "$b" =~ ^[0-9]+(\.[0-9]+)?$ ]] && printf 'env FLEET_ACTIONS_BUDGET_USD %s\n' "$b"
  return 0
}
# A busy or briefly unreachable node is asked again before the hub takes the batch (10-20: one
# ssh blip at 10:31 put a minion and its full test run on dino at load 48 while lucky sat idle).
asks="${FLEET_NODE_ASK_TRIES:-6}"
for ask in $(seq 1 "$asks"); do
  talk "minion --items $items" node_stdin
  [ "$accepted" = 1 ] && break
  [ "$ask" -lt "$asks" ] && { echo "[node_minion] node did not accept (ssh rc=$rc), ask $ask/$asks"; sleep "${FLEET_NODE_ASK_WAIT_S:-80}"; }
done
[ "$accepted" = 1 ] || run_here "node did not accept after $asks asks (ssh rc=$rc)"
tries=0
while [ "$done" = 0 ] && [ "$rc" != 0 ] && [ "$tries" -lt "${FLEET_NODE_ATTACH_TRIES:-5}" ]; do
  tries=$((tries + 1))
  echo "[node_minion] link dropped (ssh rc=$rc); re-attaching ($tries)"
  sleep "${FLEET_NODE_ATTACH_WAIT_S:-30}"
  talk "attach --items $items"
  case "$rc" in 2|5) break ;; esac  # refused (a node without attach) or node-gone: nothing to follow
done
exit "$rc"
