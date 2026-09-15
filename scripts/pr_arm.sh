#!/usr/bin/env bash
# pr_arm.sh — arm auto-merge on ONE pull request, and nothing else.
#
# dumbledore is denied `Bash(gh pr merge:*)` so it cannot merge its own governance changes
# (dumbledore.fleet.json). That deny is correct in intent and one flag too wide in effect: it
# also blocks `gh pr merge --auto`, which is NOT a merge. Arming hands the decision to the
# required checks; GitHub merges an armed PR only once every one of them passes.
#
# Measured 2026-09-14: six consecutive dumbledore PRs (fk#973/#984/#989/#992/#998/#1005) sat
# with autoMergeRequest=null for up to 3 days, four of them CLEAN and fully green, while every
# kit PR that WAS armed at creation merged ~3 minutes after opening (fk#1002/#1003/#1006/#1008/
# #1009/#1010/#1011). Nothing else would have armed them either: worktree_builder.sh arms only
# PRs it opens itself, and auto_update_branch.sh's 15-minute sweep resolves a single
# $FLEET_REPO slug — the product repo — so it has never looked at a fleet-kit PR.
#
# This wrapper exists so the capability can be granted without widening the deny: it takes a PR
# number and an optional repo, and there is no argument you can pass it that merges anything,
# picks a strategy, or reaches --admin. It is the only sanctioned arm path for a member that
# may not merge.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=merge_arm.sh
. "$HERE/merge_arm.sh"

usage() {
  echo "usage: pr_arm.sh <pr-number> [owner/repo]" >&2
  echo "  arms auto-merge only; it cannot merge, force-merge, or pick a merge strategy" >&2
}

main() {
  local pr="${1:-}" repo="${2:-}"
  case "$pr" in
    ''|*[!0-9]*) usage; return 2 ;;
  esac
  local err
  if err="$(arm_pr_auto_merge "$pr" "$repo")"; then
    echo "armed auto-merge on ${repo:+$repo}#$pr"
    return 0
  fi
  # Never swallow the arm error: a failed arm that prints nothing is the exact bug
  # worktree_builder.sh shipped for weeks (see selftest's raw-merge-ban check).
  echo "arm FAILED on ${repo:+$repo}#$pr: $err" >&2
  return 1
}

main "$@"
