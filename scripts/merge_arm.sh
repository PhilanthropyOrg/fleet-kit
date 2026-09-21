#!/usr/bin/env bash
# merge_arm.sh — one arm command for a plain (non-queue) repo.
#
# gh#524 (parent: #523 deliverable 2). auto_update_branch.sh and worktree_builder.sh each
# hardcoded ONE merge-arm shape, and each shape used to be wrong on a repo with a GitHub merge
# queue:
#
#   * bare `gh pr merge --auto`       -- was REQUIRED on a merge-queue-controlled repo. An
#     explicit strategy there was an invalid combination: gh ERRORED ("The merge strategy for
#     main is set by the merge queue") instead of enqueueing -- confirmed live on
#     nonprofit-atlas#3307/#3108.
#   * `gh pr merge --auto --squash`   -- REQUIRED on a plain (non-queue) repo: gh refuses to
#     guess a strategy non-interactively there ("--merge, --rebase, or --squash required when
#     not running interactively") -- confirmed live on fleet-kit's own repo (PRs
#     #406/#407/#413/#414/#416/#417 in one day).
#
# fleet-kit's GitHub merge queue was deleted 2026-09-21 (fleet-kit#1193, Reif: "merge queues are
# just not working for us; let the deploy puke if there are errors; remove that") -- this repo
# is a plain repo now, and the --squash fallback below is the live path. The bare-`--auto` try
# is kept first because it is still harmless (and still the right call on a repo that DOES run
# a queue, like nonprofit-atlas), so this function stays correct if a queue is ever reintroduced
# or reused against a different repo. Any failure that isn't the specific
# "required when not running interactively" string (permissions, already merged, a judge-judy
# BLOCK) is never retried into an unrelated error; it is reported as-is.
set -u

# arm_pr_auto_merge PR_NUMBER
#
# On success: prints nothing, returns 0.
# On failure: prints the failure message, returns non-zero -- a drop-in replacement for the
# `arm_err="$(gh pr merge "$pr" --auto 2>&1 >/dev/null)"` shape both callers already used.
arm_pr_auto_merge() {
  local pr="$1" repo="${2:-}" err
  # Optional second arg: an owner/repo slug, for arming a PR in a repo that is not the cwd's.
  # Callers that pass nothing keep the original one-arg behaviour exactly.
  local -a R=()
  [ -n "$repo" ] && R=(-R "$repo")
  if err="$(gh pr merge "$pr" ${R[@]+"${R[@]}"} --auto 2>&1 >/dev/null)"; then
    return 0
  fi
  if [[ "$err" == *"required when not running interactively"* ]]; then
    if err="$(gh pr merge "$pr" ${R[@]+"${R[@]}"} --auto --squash 2>&1 >/dev/null)"; then
      return 0
    fi
  fi
  printf '%s' "$err"
  return 1
}
