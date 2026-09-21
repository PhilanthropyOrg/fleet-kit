#!/usr/bin/env bash
# merge_arm.sh — one arm command for a plain (non-queue) repo.
#
# gh#524 (parent: #523 deliverable 2): auto_update_branch.sh and worktree_builder.sh each
# hardcoded ONE merge-arm shape. On a plain repo gh refuses to guess a strategy
# non-interactively ("--merge, --rebase, or --squash required when not running
# interactively"), so `--auto --squash` is the form that works -- confirmed live on fleet-kit's
# own PRs #406/#407/#413/#414/#416/#417 in one day.
#
# fk#1197 (after fk#1193): neither fleet-kit nor the product repo runs a GitHub merge queue any
# more (fleet-kit's deleted 2026-09-21, philanthropy's 2026-09-18, Reif: "merge queues are just
# not working for us"). So `--auto --squash` is tried FIRST (one round trip on every merge). The
# bare `--auto` form is kept only as the fallback for the one error a queue-controlled repo
# gives an explicit strategy ("The merge strategy for main is set by the merge queue"), so this
# function stays correct if a queue is ever reintroduced. Any other failure (permissions,
# already merged, a judge-judy BLOCK) is reported as-is, never retried into an unrelated error.
#
# Callers MUST check the exit code: a failed arm that logs as a success is a PR that never
# merges with nothing anywhere saying why (fk#524).

set -u

# arm_pr_auto_merge PR_NUMBER
#
# On success: prints nothing, returns 0.
# On failure: prints the failure message, returns non-zero -- a drop-in replacement for the
# `arm_err="$(gh pr merge "$pr" --auto 2>&1 >/dev/null)"` shape both callers already used.
arm_pr_auto_merge() {
  local pr="$1" repo="${2:-}" err
  local -a R=()
  [ -n "$repo" ] && R=(-R "$repo")
  if err="$(gh pr merge "$pr" ${R[@]+"${R[@]}"} --auto --squash 2>&1 >/dev/null)"; then
    return 0
  fi
  if [[ "$err" == *"set by the merge queue"* ]]; then
    if err="$(gh pr merge "$pr" ${R[@]+"${R[@]}"} --auto 2>&1 >/dev/null)"; then
      return 0
    fi
  fi
  printf '%s' "$err"
  return 1
}
