#!/usr/bin/env bash
# verified_test.sh -- run the suite and leave a receipt the push hook can check.
#
# The receipt is the whole point: pretest_push_hook.py refuses a `git push` out of a worktree
# whose current content has no green run behind it. Writing the receipt by hand is not a
# shortcut worth taking -- the hash comes from the hook's own --content-hash, so a
# hand-written one is wrong the moment the tree moves.
#
#   bash /fleet-kit/scripts/verified_test.sh                 # tests near the diff (tests_for_diff)
#   bash /fleet-kit/scripts/verified_test.sh tests/test_x.py # narrower, recorded as such
#
# Width defaults to the box's core count, not 4: the pool machines have 6-7 cores and the
# hosted runners this replaces had 4, so the local run should not inherit their ceiling.
set -uo pipefail

WT="${WT_PATH:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$WT" || { echo "verified_test: cannot cd to $WT" >&2; exit 2; }

HOOK="$(dirname "$(readlink -f "$0")")/pretest_push_hook.py"
RECEIPT="$(python3 "$HOOK" --receipt-path "$WT")"
WORKERS="${PYTEST_WORKERS:-auto}"

# The interpreter that can import the repo's dependencies (scripts/test_python.sh: a cached
# venv per pyproject hash). The container's own python3 has none of them, and hunting for one
# ate most of a 30-minute fixer pass twice on 2026-09-25 (#7975, #7982). The worktree's src/
# goes first on PYTHONPATH so tests run the code in THIS worktree.
TEST_PY="$(bash "$(dirname "$(readlink -f "$0")")/test_python.sh" "$WT")"
if [ -n "$TEST_PY" ] && [ "$TEST_PY" != "$(command -v python3)" ]; then
  export PATH="$(dirname "$TEST_PY"):$PATH"
  echo "verified_test: test interpreter $TEST_PY"
fi
[ -d "$WT/src" ] && export PYTHONPATH="$WT/src${PYTHONPATH:+:$PYTHONPATH}"

# Receipt is written AFTER the run, against the content as it stands THEN -- computing it up
# front would certify a tree the suite never saw if a test writes into the worktree.
rm -f "$RECEIPT"

# 2026-09-29: preflight + pytest are the CPU-heavy part; at most FLEET_TEST_SLOTS run at once on
# this box, the rest queue here in the foreground (scripts/test_slots.sh).
. "$(dirname "$(readlink -f "$0")")/test_slots.sh"
test_slot_acquire || exit 75

# fk#1166: with no arguments this ran the WHOLE suite (12 min on philanthropy). The push hook
# needs this receipt, so every minion ran the suite at least once, the harness backgrounded it
# at 120s, and 103 passes in 7 days ended with "I'll wait for the background run" -- a finished
# build never pushed. Where the repo ships its own diff-scoped runner (philanthropy's
# scripts/tests_for_diff.py, its documented pre-push law), run THAT: minutes, not twelve. It
# exits 3 when the diff is too wide to scope, and then the full suite is the honest run.
# 2026-09-25: lint and the repo's cheap CI gates first (preflight_gate.py). #7975/#7982/#7986
# went red on ruff I001/format, the repo-health ratchet and docs freshness -- all checkable in
# seconds here, none of them run before. The autofix half rewrites files BEFORE the content hash
# below is taken, so the receipt certifies the fixed tree. A red preflight is a red receipt: the
# push hook then blocks exactly as it does for a failing test. No skip switch, on purpose.
if python3 "$(dirname "$(readlink -f "$0")")/preflight_gate.py" "$WT"; then
  PREFLIGHT="pass"
else
  HASH="$(python3 "$HOOK" --content-hash "$WT")"
  printf '{"status":"fail","content":"%s","args":"preflight","exit":1,"preflight":"fail","ts":%d}\n' \
    "$HASH" "$(date +%s)" > "$RECEIPT"
  echo "verified_test: preflight (lint / repo gates) FAILED -- fix what it printed; the push hook will block until it is green" >&2
  exit 1
fi

# 2026-09-29: the product's tests write ~900 MB of throwaway SQLite per run and fsync every
# commit; on dino's shared disk they sat blocked on the journal at ~17% CPU. eatmydata turns
# fsync into a no-op for the test process only (the test DBs are deleted afterwards anyway):
# 109 tests, same box and load, 1159s/1169s -> 443s/464s. Absent binary = run as before.
EMD=(); EMD_BIN="${FLEET_EATMYDATA_BIN:-eatmydata}"
if command -v "$EMD_BIN" >/dev/null 2>&1; then EMD=("$EMD_BIN"); echo "verified_test: fsync off for the test run (eatmydata)"; fi

ARGS="${*:-full}"
NO_TESTS=""
# What counts as a test file, any stack: a tests/ test/ __tests__/ spec/ directory, test_x.*,
# x_test.*, x.test.*, x.spec.*.
TEST_FILE_RE='(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.[a-z]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$'
if [ $# -eq 0 ] && [ -f scripts/tests_for_diff.py ]; then
  echo "verified_test: diff-scoped -- python3 scripts/tests_for_diff.py --run in $WT"
  ${EMD[@]+"${EMD[@]}"} python3 scripts/tests_for_diff.py --run
  code=$?
  ARGS="tests_for_diff"
  if [ "$code" -eq 3 ]; then
    # 2026-09-29 (Reif): the box runs targeted tests only, never the whole suite. The full suite
    # already runs in the product repo's CI on every PR; a 30-min local copy of it held a test
    # slot, starved every other pass, and timed passes out. A diff too wide to scope pushes on
    # preflight alone and CI is its test run -- the pass then waits for CI (pr_ci_wait.py).
    echo "verified_test: diff too wide to scope (rc=3) -- no local run; the full suite runs in CI on the PR"
    code=0
    ARGS="ci-only (diff too wide to scope; full suite runs in CI on the PR)"
  fi
elif [ $# -eq 0 ] && TREE_FILES="$(git ls-files --cached --others --exclude-standard 2>/dev/null)" && ! grep -qiE "$TEST_FILE_RE" <<<"$TREE_FILES"; then
  # A brand-new product repo (fleet_init.py) has no tests and no runner yet. pytest then exits 5
  # ("no tests collected"), or is not installed at all, the receipt is red, and the push hook
  # blocks the very PRs that would add the first test. Only when there is nothing to run: no
  # arguments, no scripts/tests_for_diff.py, and not one test-shaped file in the tree. The
  # receipt lets the push through and says in `args` that NO test ran; preflight above still did.
  # If git cannot list the tree this is NOT taken: unknown is never "no tests".
  echo "verified_test: this repo has no tests yet (no scripts/tests_for_diff.py, no test files) -- nothing to run"
  code=0
  NO_TESTS=1
  ARGS="no-tests (new repo: no test files and no scripts/tests_for_diff.py; nothing ran)"
else
  echo "verified_test: pytest -n $WORKERS ${*:-<full suite>} in $WT"
  ${EMD[@]+"${EMD[@]}"} python3 -m pytest -q -n "$WORKERS" "$@"
  code=$?
fi

HASH="$(python3 "$HOOK" --content-hash "$WT")"
if [ "$code" -eq 0 ]; then STATUS=pass; else STATUS=fail; fi
printf '{"status":"%s","content":"%s","args":"%s","exit":%d,"preflight":"%s","ts":%d}\n' \
  "$STATUS" "$HASH" "$ARGS" "$code" "$PREFLIGHT" "$(date +%s)" > "$RECEIPT"

[ "$code" -eq 0 ] && [ -n "$NO_TESTS" ] && echo "verified_test: NOTHING TO TEST ($ARGS). Evidence line: \`verified_test.sh\` found no tests in this repo; nothing was test-verified."
[ "$code" -eq 0 ] && [ -z "$NO_TESTS" ] && echo "verified_test: PASS ($ARGS). The full suite runs in CI on the PR -- evidence line: \`verified_test.sh\` $ARGS green; full suite in CI on the PR."
if [ "$code" -ne 0 ]; then
  echo "verified_test: suite FAILED (exit $code) -- the push hook will block until it is green" >&2
fi
exit "$code"
