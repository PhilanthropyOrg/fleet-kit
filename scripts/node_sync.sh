#!/usr/bin/env bash
# node_sync.sh -- keep a worker node's container on the kit's origin/main. Runs ON the node,
# from its own crontab (*/5, installed by node_up.sh), and once by node_up.sh itself. Idempotent.
#
# A worker has no gru, dashboard or cron of its own (entrypoint.sh FLEET_NODE_ROLE=worker), so
# the hub's auto_deploy.sh blue-green machinery is more than it needs: when origin/main moves
# and no minion is running here, rebuild and replace. A running minion is never cut over; the
# next tick tries again.
#
# Reads ~/.config/fleet-kit/node.env (FLEET_NODE_INSTANCE, FLEET_NODE_CONTAINER,
# FLEET_NODE_SLOTS) and ~/.config/fleet-kit/gh_token.
set -uo pipefail
NODE_ENV="${FLEET_NODE_ENV:-$HOME/.config/fleet-kit/node.env}"
[ -f "$NODE_ENV" ] || { echo "[node_sync] no $NODE_ENV -- run node_up.sh first" >&2; exit 1; }
. "$NODE_ENV"
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTANCE="${FLEET_NODE_INSTANCE:?}"
CONTAINER="${FLEET_NODE_CONTAINER:-fleet-worker}"
INSTANCE_DIR="$KIT_DIR/instances/$INSTANCE"
IMAGE="fleet-kit:$INSTANCE-worker"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

cd "$KIT_DIR"
exec 9>"$KIT_DIR/.git/.node_sync.lock"
flock -n 9 || exit 0

git fetch -q origin main || { echo "$(ts) [node_sync] fetch failed -- keeping current build"; exit 0; }
want="$(git rev-parse origin/main)"
have="$(podman inspect -f '{{index .Config.Labels "fleet.sha"}}' "$CONTAINER" 2>/dev/null || true)"
running="$(podman inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)"
[ "$want" = "$have" ] && [ "$running" = "true" ] && exit 0

if [ "$running" = "true" ] && podman exec "$CONTAINER" pgrep -f "run_member.sh minion" >/dev/null 2>&1; then
  echo "$(ts) [node_sync] ${have:0:7} -> ${want:0:7} waiting: a minion is running"
  exit 0
fi

git merge -q --ff-only origin/main || { echo "$(ts) [node_sync] checkout is not fast-forwardable to origin/main -- fix by hand" >&2; exit 1; }
echo "$(ts) [node_sync] building ${want:0:7}"
podman build -q --build-arg DEPLOY_SHA="$want" -t "$IMAGE" . >/dev/null || { echo "$(ts) [node_sync] build FAILED" >&2; exit 1; }

set -a; . "$INSTANCE_DIR/fleet.env"; set +a
mounts=()
for acct in ${FLEET_ACCOUNTS:-primary}; do
  mounts+=(-v "$HOME/.claude-$acct:/root/.claude-$acct")
done
mkdir -p "$INSTANCE_DIR/repo" "$INSTANCE_DIR/logs"
# -e GH_TOKEN with no value: podman copies it from this environment, so it is never in argv.
GH_TOKEN="$(cat "$HOME/.config/fleet-kit/gh_token")" podman run -d --replace --name "$CONTAINER" \
  --label "fleet.sha=$want" \
  -e GH_TOKEN \
  -e FLEET_NODE_ROLE=worker \
  -e FLEET_REPO=/repo \
  -e FLEET_REPO_URL="$FLEET_REPO_URL" \
  -e FLEET_ENV_FILE=/fleet-kit/fleet.env \
  -e FLEET_INSTANCE_NAME="$INSTANCE" \
  -v "$INSTANCE_DIR/repo:/repo" \
  -v "$INSTANCE_DIR/logs:/var/log/fleet-kit" \
  -v "$INSTANCE_DIR/fleet.env:/fleet-kit/fleet.env:ro" \
  "${mounts[@]}" \
  "$IMAGE" cron-foreground >/dev/null || { echo "$(ts) [node_sync] run FAILED" >&2; exit 1; }
echo "$(ts) [node_sync] $CONTAINER now on ${want:0:7}"
