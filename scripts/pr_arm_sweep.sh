#!/usr/bin/env bash
# pr_arm_sweep.sh -- every 20 minutes (entrypoint.sh crontab): arm auto-merge on every open PR
# that is green on its branch's own required checks and that nothing has armed.
#
# WHY THIS IS A CRON AND NOT A CHARTER LINE. Arming is minion.md step 9, i.e. an instruction an
# LLM has to reach the end of its pass to obey. Measured 2026-09-12: all 3 open fleet-kit PRs sat
# unarmed and already green on `selftest`, the branch's only required check -- two of them for 26
# hours, and two of them were fixes for the very dispatch loop below. Unmerged PRs do not
# autoclose their issues, so vp_due/gru keep dispatching builders at work that is already done:
# 61 of minion's 127 productive passes that day ended "already fixed by an open PR." See
# pr_arm_sweep.py for the rule and the rest of the measurement.
#
# This only ARMS. GitHub's auto-merge still waits for every required check, so nothing merges
# here that would not have merged had the authoring pass remembered its own step 9.
set -u
KIT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
. "$KIT/scripts/merge_arm.sh"
log() { echo "[$(date -u '+%F %T UTC')] pr_arm_sweep: $*"; }

# Both repos this instance owns: the product repo and the kit itself. Slugs are derived the same
# way self_improve_score.sh derives them -- git remote first, KIT_REPO_SLUG from fleet.env for
# the vendored /fleet-kit copy that has no .git at all (fk#178).
_repo_slug() {
  local url first_remote
  url="$(git -C "$1" remote get-url origin 2>/dev/null)"
  if [ -z "$url" ]; then
    first_remote="$(git -C "$1" remote 2>/dev/null | head -1)"
    [ -n "$first_remote" ] && url="$(git -C "$1" remote get-url "$first_remote" 2>/dev/null)"
  fi
  printf '%s' "$url" | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##'
}
PRODUCT_SLUG="${FLEET_REPO_SLUG:-$(_repo_slug "${FLEET_REPO:-}")}"
KIT_SLUG="${KIT_REPO_SLUG:-$(_repo_slug "$KIT")}"
REPOS="${FLEET_PR_ARM_REPOS:-$PRODUCT_SLUG $KIT_SLUG}"
# The kit slug can equal the product slug on a single-repo instance -- sweeping it twice would
# just re-read an already-armed list, but it also double-logs, so dedupe.
if [ "$PRODUCT_SLUG" = "$KIT_SLUG" ] && [ -z "${FLEET_PR_ARM_REPOS:-}" ]; then
  REPOS="$PRODUCT_SLUG"
fi
if [ -z "${REPOS// /}" ]; then
  log "no repos configured (set FLEET_PR_ARM_REPOS, or FLEET_REPO to a checkout with a remote) -- nothing to do"
  exit 0
fi

for repo in $REPOS; do
  [ -n "$repo" ] || continue
  out="$(python3 "$KIT/scripts/pr_arm_sweep.py" --repo "$repo" 2>&1)" || {
    log "$repo: pr_arm_sweep.py failed: ${out:0:200}"; continue; }
  arm="$(printf '%s' "$out" | python3 -c 'import sys,json; print(" ".join(str(n) for n in json.load(sys.stdin)["arm"]))' 2>/dev/null)"
  if [ -z "${arm// /}" ]; then
    log "$repo: nothing to arm"
    continue
  fi
  for pr in $arm; do
    # merge_arm.sh picks the strategy flag: a bare --auto on a merge-queue branch, --squash on a
    # plain one. CHECK THE EXIT CODE -- worktree_builder.sh's own comment records what happens
    # when a failed arm is logged as a successful one (the PR simply never merges, silently).
    if arm_err="$(arm_pr_auto_merge "$pr")"; then
      log "$repo: armed auto-merge on #$pr"
    else
      log "$repo: ARMING #$pr FAILED -- it will not merge on green: ${arm_err:-unknown error}"
    fi
  done
done
exit 0
