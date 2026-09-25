#!/usr/bin/env bash
# verified_test.sh -- run the suite and leave a receipt the push hook can check.
#
# The receipt is the whole point: pretest_push_hook.py refuses a `git push` out of a worktree
# whose current content has no green run behind it. Writing the receipt by hand is not a
# shortcut worth taking -- the hash comes from the hook's own --content-hash, so a
# hand-written one is wrong the moment the tree moves.
#
#   bash /fleet-kit/scripts/verified_test.sh                 # whole suite
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

ARGS="${*:-full}"
if [ $# -eq 0 ] && [ -f scripts/tests_for_diff.py ]; then
  echo "verified_test: diff-scoped -- python3 scripts/tests_for_diff.py --run in $WT"
  python3 scripts/tests_for_diff.py --run
  code=$?
  ARGS="tests_for_diff"
  if [ "$code" -eq 3 ]; then
    echo "verified_test: diff too wide to scope (rc=3) -- falling back to the full suite"
    python3 -m pytest -q -n "$WORKERS"
    code=$?
    ARGS="full (tests_for_diff rc=3)"
  fi
else
  echo "verified_test: pytest -n $WORKERS ${*:-<full suite>} in $WT"
  python3 -m pytest -q -n "$WORKERS" "$@"
  code=$?
fi

HASH="$(python3 "$HOOK" --content-hash "$WT")"
if [ "$code" -eq 0 ]; then STATUS=pass; else STATUS=fail; fi
printf '{"status":"%s","content":"%s","args":"%s","exit":%d,"preflight":"%s","ts":%d}\n' \
  "$STATUS" "$HASH" "$ARGS" "$code" "$PREFLIGHT" "$(date +%s)" > "$RECEIPT"

if [ "$code" -ne 0 ]; then
  echo "verified_test: suite FAILED (exit $code) -- the push hook will block until it is green" >&2
fi
exit "$code"
