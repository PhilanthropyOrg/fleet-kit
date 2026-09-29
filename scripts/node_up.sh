#!/usr/bin/env bash
# node_up.sh -- add a worker node to a hub's fleet. Run from the OPERATOR's machine (the one that
# can `ssh` both boxes). Idempotent: re-run it to re-sync fleet.env and credentials.
#
#   bash scripts/node_up.sh --hub dino --node lucky --instance nonprofit-atlas \
#        --accounts tgp [--slots 3] [--container philanthropy-worker] [--hub-kit fleet-kit]
#
# Shape (why not a second full fleet): the hub's gru stays the only planner and claimer. It
# dispatches claimed minion batches; dispatch_member.sh sends them over ssh to the node, whose
# authorized_keys forces node_gate.sh (`minion --items <n,..>` only). See node_gate.sh.
#
# What it places (secrets streamed hub -> this machine -> node through ssh pipes, never printed,
# never on argv, written 0600):
#   node: ~/.claude-<acct>/.credentials.json   (each --accounts name, copied from the hub)
#   node: ~/.config/fleet-kit/gh_token          (the hub's `gh auth token`)
#   node: ~/<kit>/instances/<instance>/fleet.env (the hub's, + FLEET_ACCOUNTS=<--accounts>)
#   node: ~/.config/fleet-kit/node.env          (instance, container, slots; no secrets)
#   node: ~/.ssh/authorized_keys                 (+1 line: restrict,command=node_gate.sh)
#   node: crontab                                (+1 line: node_sync.sh every 5 min)
#   hub:  ~/.config/fleet-kit/node/{id_ed25519,id_ed25519.pub,config,known_hosts}
#
# Only non-rotating credentials are copied (a `claude setup-token` token: no refreshToken). A
# rotating OAuth login refreshed on two boxes invalidates the other box's copy, and the hub's
# whole fleet would 401. The script refuses those.
#
# Undo: hub `rm -rf ~/.config/fleet-kit/node` (minions run on the hub again at once); node
# `podman rm -f <container>`, drop the node_sync.sh crontab line and the node_gate.sh key line.
set -euo pipefail
HUB="" NODE="" INSTANCE="" ACCOUNTS="" SLOTS=3 CONTAINER="" HUB_KIT="fleet-kit"
while [ $# -gt 0 ]; do
  case "$1" in
    --hub) HUB="$2"; shift 2 ;;
    --node) NODE="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --accounts) ACCOUNTS="$2"; shift 2 ;;
    --slots) SLOTS="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --hub-kit) HUB_KIT="$2"; shift 2 ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
done
[ -n "$HUB" ] && [ -n "$NODE" ] && [ -n "$INSTANCE" ] && [ -n "$ACCOUNTS" ] \
  || { echo "usage: node_up.sh --hub <ssh> --node <ssh> --instance <name> --accounts \"a b\" [--slots N]" >&2; exit 2; }
CONTAINER="${CONTAINER:-fleet-worker-$INSTANCE}"
say() { echo "[node_up] $*"; }

# How the hub reaches the node: this machine's own ssh config for it (host, user, proxy).
G="$(ssh -G "$NODE")"
N_HOST="$(awk '$1=="hostname"{print $2}' <<<"$G")"
N_USER="$(awk '$1=="user"{print $2}' <<<"$G")"
N_PROXY="$(awk '$1=="proxycommand"{$1=""; sub(/^ /,""); print}' <<<"$G" | sed "s/%h/$N_HOST/g")"

say "1/6 node: kit checkout"
ssh "$NODE" "test -d ~/fleet-kit/.git || git clone -q https://github.com/PhilanthropyOrg/fleet-kit.git ~/fleet-kit; cd ~/fleet-kit && git fetch -q origin main && git merge -q --ff-only origin/main"

say "2/6 secrets hub -> node (streamed, not printed)"
for acct in $ACCOUNTS; do
  rotating="$(ssh "$HUB" "python3 -c 'import json,os; d=json.load(open(os.path.expanduser(\"~/.claude-$acct/.credentials.json\"))); print(bool(d.get(\"claudeAiOauth\", d).get(\"refreshToken\")))'")"
  if [ "$rotating" != "False" ]; then
    echo "[node_up] REFUSED: account '$acct' on $HUB has a rotating OAuth login (refreshToken). Copying it would log the hub out when either box refreshes. Use a 'claude setup-token' account." >&2
    exit 1
  fi
  ssh "$HUB" "cat ~/.claude-$acct/.credentials.json" \
    | ssh "$NODE" "umask 077; mkdir -p ~/.claude-$acct && cat > ~/.claude-$acct/.credentials.json"
  say "   placed $NODE:~/.claude-$acct/.credentials.json"
done
ssh "$HUB" "gh auth token" | ssh "$NODE" "umask 077; mkdir -p ~/.config/fleet-kit && cat > ~/.config/fleet-kit/gh_token"
say "   placed $NODE:~/.config/fleet-kit/gh_token"
{ ssh "$HUB" "cat ~/$HUB_KIT/instances/$INSTANCE/fleet.env"
  printf '\n# --- worker node overrides (node_up.sh) ---\nFLEET_ACCOUNTS="%s"\n' "$ACCOUNTS"
} | ssh "$NODE" "umask 077; mkdir -p ~/fleet-kit/instances/$INSTANCE && cat > ~/fleet-kit/instances/$INSTANCE/fleet.env"
say "   placed $NODE:~/fleet-kit/instances/$INSTANCE/fleet.env"
ssh "$NODE" "umask 077; printf 'FLEET_NODE_INSTANCE=%s\nFLEET_NODE_CONTAINER=%s\nFLEET_NODE_SLOTS=%s\n' '$INSTANCE' '$CONTAINER' '$SLOTS' > ~/.config/fleet-kit/node.env"

say "3/6 hub: node key + ssh config"
D='~/.config/fleet-kit/node'
ssh "$HUB" "mkdir -p $D && chmod 700 $D && { test -f $D/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -C 'fleet-hub-$HUB' -f $D/id_ed25519; }"
ssh "$HUB" "cat > $D/config" <<EOF
# node_up.sh: paths are the hub CONTAINER's (deploy.sh mounts this dir at /root/.fleet-node).
Host fleet-node
  HostName $N_HOST
  User $N_USER
  ProxyCommand $N_PROXY
  IdentityFile /root/.fleet-node/id_ed25519
  IdentitiesOnly yes
  UserKnownHostsFile /root/.fleet-node/known_hosts
  StrictHostKeyChecking yes
  ServerAliveInterval 30
  ServerAliveCountMax 10
EOF
ssh "$HUB" "chmod 600 $D/config"  # ssh rejects a group-writable config ("Bad owner or permissions")
PUB="$(ssh "$HUB" "cat $D/id_ed25519.pub")"

say "4/6 node: forced-command key"
ssh "$NODE" "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && { grep -qF '$(awk '{print $2}' <<<"$PUB")' ~/.ssh/authorized_keys || echo 'restrict,command=\"'\$HOME'/fleet-kit/scripts/node_gate.sh\" $PUB' >> ~/.ssh/authorized_keys; }"

say "5/6 node: build + start $CONTAINER, sync cron"
ssh "$NODE" "sudo -n loginctl enable-linger \$(id -un) || echo '[node_up] WARNING: could not enable linger; cron podman calls will not see the container'; bash ~/fleet-kit/scripts/node_sync.sh; mkdir -p ~/fleet-kit/instances/$INSTANCE/logs; line='*/5 * * * * bash \$HOME/fleet-kit/scripts/node_sync.sh >> \$HOME/fleet-kit/instances/$INSTANCE/logs/node_sync.log 2>&1'; crontab -l 2>/dev/null | grep -qF node_sync.sh || { crontab -l 2>/dev/null; echo \"\$line\"; } | crontab -"

say "6/6 hub -> node path (expects node-refused: the gate only runs minion batches)"
ssh "$HUB" "ssh -F none -i $D/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=$D/known_hosts -o StrictHostKeyChecking=accept-new -o 'ProxyCommand=$N_PROXY' $N_USER@$N_HOST probe" || true
say "done. The hub container picks up ~/.config/fleet-kit/node on its next deploy (deploy.sh mounts it)."
