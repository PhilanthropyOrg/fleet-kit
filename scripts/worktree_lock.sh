# worktree_lock.sh -- shared fair lock for `git worktree add`/`remove`/`prune` against $REPO's
# .git/worktrees admin dir. Sourced by run_member.sh and worktree_builder.sh, which contend for
# the SAME lock (gh#4727: an unguarded `remove`/`prune` races a sibling's concurrent `add`).
#
# gh#672: replaces the old `mkdir "$LOCK"` + `sleep 2` spin. That spin has no memory of arrival
# order -- a lock release and a brand-new contender's very FIRST `mkdir` attempt can land in the
# same instant and win exactly as often as a process that has been polling for minutes, because
# a sleeping poller isn't even attempting the `mkdir` at the moment the lock frees. Measured
# 2026-09-07: under ~30 concurrent run_member.sh processes (load 35 on 7 cores), this starved
# the messenger inbox pass (fk#669/#670 -- the fleet's highest-priority input, a real reply from
# Reif) for 6 minutes straight before it hit the 120s per-attempt timeout and died with FATAL.
#
# flock(2) fixes the mechanism, not just the symptom: once a waiter calls it, that waiter is a
# real blocked kernel task for the ENTIRE wait, not a process that stops contending between
# polls -- so a late arrival can no longer win purely by having better timing against someone
# else's sleep. Stale-lock recovery (a sibling killed mid-add wedging every later attempt) is
# also the kernel's problem now: flock releases automatically when the holding process dies, so
# the old "steal a lock dir older than 5 minutes" workaround is no longer needed.
WORKTREE_LOCK_FILE="${TMPDIR:-/tmp}/fleet-kit-worktree-add.flock"

# gh#5632: 20 of 32 concurrently-spawned minions died `FATAL: could not create isolated
# worktree` in a ~90s window. Not a broken lock -- an under-provisioned one: the fixed 120s
# per-attempt budget below was sized for ordinary (few-at-a-time) concurrency. A `git worktree
# add` critical section (fetch + prune + add) measured 10-20+s under contention, so once ~7+
# processes are queued on the SAME flock at once, the process at the back can need longer than
# 120s just for the queue ahead of it to clear -- before its own attempt has a chance to matter.
# Confirmed at concurrency as low as 3-8 over the two days before the 32-wide fanout made it
# unmissable.
#
# Fix: scale the wait budget to how many OTHER processes are ALSO mid-attempt on this lock right
# now -- a live count on disk, not a config knob a caller has to know or pass -- so the timeout
# tracks whatever concurrency a given pass actually produces instead of a number tuned for a
# different one. At ordinary (single/few-minion) concurrency the scaled value is smaller than
# the caller's own timeout, so the caller's number wins unchanged (AC: "existing single/few-
# minion concurrency continues to work unchanged"). Only budgets >= WORKTREE_LOCK_SCALE_MIN_TIMEOUT
# scale at all -- run_member.sh's/worktree_builder.sh's exit-time cleanup call
# (`worktree_lock_acquire 30`) sits below that on purpose and is left exactly as bounded as it
# already was; a stuck cleanup must still not wedge the pass from finishing, and reworking that
# is explicitly out of scope for this fix.
WORKTREE_LOCK_WAITERS_DIR="${TMPDIR:-/tmp}/fleet-kit-worktree-add.waiters"
WORKTREE_LOCK_SEC_PER_WAITER="${WORKTREE_LOCK_SEC_PER_WAITER:-20}"
WORKTREE_LOCK_SCALE_MIN_TIMEOUT="${WORKTREE_LOCK_SCALE_MIN_TIMEOUT:-60}"
WORKTREE_LOCK_MAX_TIMEOUT="${WORKTREE_LOCK_MAX_TIMEOUT:-1200}"

# worktree_lock_timeout_for base_timeout_s: prints the effective wait budget for a request of
# base_timeout_s seconds. Pure function of the waiters dir + env, callable standalone for tests.
worktree_lock_timeout_for() {
  local base="$1"
  if [ "$base" -lt "$WORKTREE_LOCK_SCALE_MIN_TIMEOUT" ]; then
    echo "$base"
    return
  fi
  local waiters scaled
  # -mmin -10: a marker from a process that died mid-attempt (kill -9, OOM) without reaching
  # worktree_lock_release would otherwise leak forever and inflate every later estimate; capping
  # at 10 minutes self-heals that without needing anyone to clean the dir up.
  waiters=$(find "$WORKTREE_LOCK_WAITERS_DIR" -type f -mmin -10 2>/dev/null | wc -l)
  [ "$waiters" -lt 1 ] && waiters=1
  scaled=$(awk -v w="$waiters" -v s="$WORKTREE_LOCK_SEC_PER_WAITER" 'BEGIN { printf "%d", (w * s) + 0.999 }')
  [ "$scaled" -gt "$WORKTREE_LOCK_MAX_TIMEOUT" ] && scaled="$WORKTREE_LOCK_MAX_TIMEOUT"
  if [ "$scaled" -gt "$base" ]; then echo "$scaled"; else echo "$base"; fi
}

# worktree_lock_acquire [timeout_s=120]: blocks on the kernel's wait queue (not a poll loop)
# until the lock is free, or returns 1 after the (possibly scaled, see above) timeout. Opens the
# lock on fd 8 for the life of the caller's shell -- release explicitly with
# worktree_lock_release before anything that must not inherit it (podman run, a
# backgrounded/detached child), the same discipline auto_deploy.sh already follows for its own
# flock fd 9.
worktree_lock_acquire() {
  local base="${1:-120}"
  mkdir -p "$WORKTREE_LOCK_WAITERS_DIR" 2>/dev/null || true
  : > "$WORKTREE_LOCK_WAITERS_DIR/$$" 2>/dev/null || true
  local timeout
  timeout=$(worktree_lock_timeout_for "$base")
  exec 8>"$WORKTREE_LOCK_FILE"
  flock -w "$timeout" 8
}

# worktree_lock_release: always safe to call, even if acquire never ran or already timed out.
worktree_lock_release() {
  flock -u 8 2>/dev/null || true
  exec 8>&- 2>/dev/null || true
  rm -f "$WORKTREE_LOCK_WAITERS_DIR/$$" 2>/dev/null || true
}
