# test_slots.sh -- at most N verified_test.sh runs execute at once on this box; the rest queue.
#
# dino is a 7-core box with ~15 `claude -p` passes on it. The passes are mostly waiting on the
# API; their test runs are not. 2026-09-29: load 12-21, a 25-file scoped run took 333s serially
# and no faster with xdist, and one 457-test run took 24 min. Every run slowed every other one,
# and minion passes timed out at 90 min. Concurrency of passes stays as is (throughput); only
# the CPU-heavy part -- preflight + pytest -- takes a slot.
#
# Same slot-file + flock pattern as claude_concurrency.sh. The wait is in the FOREGROUND of the
# caller's Bash call and bounded: a minion's Bash call has a 30 min limit (run_member.sh), so
# waiting longer than FLEET_TEST_SLOT_WAIT_S (default 900s) fails fast with a clear message
# instead of hanging until the harness kills the call. Progress lines go to stdout so the model
# sees it is queued, not stuck.
TEST_SLOT_DIR="${FLEET_TEST_SLOT_DIR:-/tmp/fleet-kit-test-slots}"
TEST_SLOT_N="${FLEET_TEST_SLOTS:-$(( $(nproc 2>/dev/null || echo 4) / 2 ))}"
[ "$TEST_SLOT_N" -ge 1 ] 2>/dev/null || TEST_SLOT_N=1
# 900s, not 600 (2026-09-29, 6h live): runs held a slot p75 458s / p90 743s, so a 600s wait gave
# up 17 times against 25 successes -- each give-up a retry that queued all over again.
TEST_SLOT_WAIT_S="${FLEET_TEST_SLOT_WAIT_S:-900}"
TEST_SLOT_PROGRESS_S="${FLEET_TEST_SLOT_PROGRESS_S:-30}"

# test_slot_acquire: holds one slot on fd 8 for the life of the caller's shell. Returns 0 with
# a slot, 1 (after printing why) when none freed within TEST_SLOT_WAIT_S.
test_slot_acquire() {
  local n="$TEST_SLOT_N" i start now next
  mkdir -p "$TEST_SLOT_DIR" 2>/dev/null || true
  start=$(date +%s); next=$start
  while :; do
    for ((i = 0; i < n; i++)); do
      exec 8>"$TEST_SLOT_DIR/slot-$i.lock"
      if flock -n 8; then
        echo "verified_test: got test slot $((i + 1))/$n after $(( $(date +%s) - start ))s"
        return 0
      fi
      exec 8>&-
    done
    now=$(date +%s)
    if [ $(( now - start )) -ge "$TEST_SLOT_WAIT_S" ]; then
      echo "verified_test: NO TEST SLOT -- all $n slots stayed busy for ${TEST_SLOT_WAIT_S}s (other passes' test runs). Nothing ran and no receipt was written. Re-run this same command; it queues again." >&2
      return 1
    fi
    if [ "$now" -ge "$next" ]; then
      echo "verified_test: waiting for test slot -- all $n busy, waited $(( now - start ))s of ${TEST_SLOT_WAIT_S}s"
      next=$(( now + TEST_SLOT_PROGRESS_S ))
    fi
    sleep 1
  done
}
