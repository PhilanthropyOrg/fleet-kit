#!/bin/bash
# custodian.sh -- one surface per job, driven down (fk#807). A plain script, not a model pass.
#
# WHY A SCRIPT: the debt is arithmetic over the route table and the work item is generated from
# it. There is no judgment for a model to add, so this follows roomba's shape (fleet-kit#514):
# run_member.sh dispatches here via the spec's llm.runner, and the pass is recorded through
# run_report.py so fleet.db, /status and fleet_kpi see it exactly like any other member.
#
# THE RECURSION is machinery that already exists, not anything this script re-decides:
#   surface_debt names the worst job -> one retirement filed -> a minion folds the extra
#   surface into the survivor and reruns ui_surfaces.py --baseline in that same PR ->
#   test_ui_surfaces_ratchet.py freezes the lower number -> the next pass reads a smaller debt.
# Terminates at debt 0, which is every job serving exactly one page.
#
# ONE AT A TIME on purpose: if a previous surface-debt item is still open, this files nothing.
# A 13-item cleanup epic is exactly the garbage nobody picks up.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
REPO="${FLEET_REPO:?set FLEET_REPO -- the product repo whose surfaces this sweeps}"
RUN_ID="custodian-$$-$(date -u +%s)"
LOG="$LOG_DIR/custodian.log"
MARKER="surface-debt"
mkdir -p "$LOG_DIR"

report() { # <outcome> <evidence> <self-critique> <exit-code>
  printf 'Outcome: %s\nEvidence: %s\nSelf-critique: %s\n' "$1" "$2" "$3" | python3 "$KIT_DIR/scripts/run_report.py" \
    --member custodian --run-id "$RUN_ID" --kind shell --exit-code "${4:-0}" \
    --pass-file - >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
}

# --- the rework numbers, first: they must refresh even when there is no surface to retire,
# --- because dumbledore's predictions ledger resolves rework_pct/churn_ratio off this cache
# --- and a stale cache reads as "unavailable" rather than resolving a row either way.
REPO_SLUG="${KIT_REPO_SLUG:-}"
if [ -z "$REPO_SLUG" ]; then
  REPO_SLUG=$(cd "$REPO" && git config --get remote.origin.url 2>/dev/null \
    | sed -E 's#(git@|https://)github.com[:/]##; s#\.git$##')
fi
REWORK_LINE="rework cache not refreshed (no repo slug resolved)"
if [ -n "$REPO_SLUG" ]; then
  if RW=$(python3 "$KIT_DIR/scripts/rework_collect.py" --repo "$REPO_SLUG" --limit 400 --print 2>&1); then
    REWORK_LINE=$(printf '%s' "$RW" | head -n 2 | tr '\n' ' ' | cut -c1-200)
  else
    REWORK_LINE="rework_collect failed: $(printf '%s' "$RW" | tail -n 1 | cut -c1-140)"
  fi
fi

# --- surface debt ---------------------------------------------------------------------------
DEBT_JSON=$(cd "$REPO" && python3 scripts/qa/surface_debt.py --json 2>&1); RC=$?
if [ "$RC" -ne 0 ]; then
  # A debt this member cannot compute is never guessed at. A fabricated 0 would read as
  # "no duplication left" and silently retire the whole loop.
  report "QUIET — surface debt could not be computed (surface_debt.py rc=$RC); nothing filed" \
         "$(printf '%s' "$DEBT_JSON" | tail -n 2 | tr '\n' ' ' | cut -c1-260) | $REWORK_LINE" \
         "escalation path: the route walker or app deps are broken, not the surfaces" "$RC"
  echo "QUIET surface_debt rc=$RC"
  exit 0
fi

DEBT=$(printf '%s' "$DEBT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("debt", 0))' 2>/dev/null || echo 0)
WORST=$(printf '%s' "$DEBT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("worst") or "")' 2>/dev/null || echo "")
UNCLASS=$(printf '%s' "$DEBT_JSON" | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin).get("unclassified") or []))' 2>/dev/null || echo "")

if [ "${DEBT:-0}" = "0" ] || [ -z "$WORST" ]; then
  report "QUIET — surface debt 0; every job serves exactly one page" \
         "unclassified jobs: ${UNCLASS:-none} | $REWORK_LINE" \
         "none -- deterministic count over the route table" 0
  echo "QUIET debt=0"
  exit 0
fi

# One retirement in flight at a time.
OPEN=$(cd "$REPO" && timeout 25s gh issue list --state open --search "in:title $MARKER" --limit 5 \
  --json number --jq '.[0].number' 2>/dev/null || true)
if [ -n "$OPEN" ] && [ "$OPEN" != "null" ]; then
  report "QUIET — surface debt $DEBT but #$OPEN is still open; one retirement at a time" \
         "worst job: $WORST | unclassified: ${UNCLASS:-none} | $REWORK_LINE" \
         "none -- filing a second item would build the backlog this member exists to prevent" 0
  echo "QUIET debt=$DEBT open=#$OPEN"
  exit 0
fi

BODY=$(mktemp); trap 'rm -f "$BODY"' EXIT
if ! (cd "$REPO" && python3 scripts/qa/surface_debt.py --next) > "$BODY" 2>>"$LOG"; then
  report "QUIET — surface debt $DEBT but the work item could not be generated; nothing filed" \
         "worst job: $WORST | $REWORK_LINE" "surface_debt.py --next failed; see $LOG" 1
  echo "QUIET --next failed"
  exit 0
fi
{
  echo
  echo "---"
  echo "Filed by the \`custodian\` member. Surface debt is **$DEBT** extra surfaces right now;"
  echo "this item retires one of them. The next pass files the next worst job only after this"
  echo "one lands, so the queue never grows a backlog nobody picks up."
} >> "$BODY"

TITLE="$MARKER: $WORST serves more than one surface, retire the extras"
NEW=$(cd "$REPO" && timeout 40s gh issue create --title "$TITLE" --body-file "$BODY" \
  --label fleet:backlog --label quality:solid 2>&1 | tail -n 1)
case "$NEW" in
  http*)
    report "filed one surface retirement: $WORST (surface debt $DEBT)" \
           "$NEW | unclassified: ${UNCLASS:-none} | $REWORK_LINE" \
           "none -- the ratchet freezes the lower count once this lands, so the next pass starts smaller" 0
    echo "OK filed $NEW"
    ;;
  *)
    report "QUIET — surface debt $DEBT but filing failed; nothing was created" \
           "gh issue create said: $(printf '%s' "$NEW" | cut -c1-200) | $REWORK_LINE" \
           "the next tick retries; no partial state is left behind" 1
    echo "QUIET file failed"
    ;;
esac
# --- SECOND AXIS: dead code ------------------------------------------------------------------
# Reif, 2026-09-12: "actively deleting code that should not exist" -- and "I'd rather it be in the
# fleet vs on the box and have it owned by one of our members." This is that axis, in the same
# shape as surface debt above: a deterministic count, ONE item filed at a time, nothing deleted by
# this member. It lands here rather than in a new member because the charter already says it --
# "either removing them or perfecting them... it needs to be recursive" -- one level down from
# surfaces.
#
# WHY NOT THE BOX CRON: scripts/box/crontab line 518 runs maint_dead_code.py weekly on atlas-serve.
# It has printed "no dead code detected" since 2026-07-15 while 42 orphans accumulated, because
# neither pyflakes nor ruff is installed there and the tool cannot tell "clean" from "I could not
# look". Measured on dino 2026-09-12: ANALYZER UNAVAILABLE, orphan scripts 43. The fleet has the
# repo, the gates and the PR path; the box has none of them.
#
# NEVER AUTO-DELETES, and that is not caution for its own sake: the 2026-09-12 scan listed
# scripts/fleet/marie_collapse.py as an orphan in the same hour it was wired into marie's PART E.
# It was uncalled only because its caller lives in the fleet-kit repo, which this scan cannot see.
# An auto-deleter would have removed the fix for the largest measured source of wasted spend.
# Orphans are PROPOSED here; a human-reviewed PR removes them.
DEAD_MARKER="dead-code"
ORPHANS=$(cd "$REPO" && timeout 120s python3 -c '
import sys
from pathlib import Path
sys.path.insert(0, "scripts")
try:
    import dead_code_lib as d
except Exception as e:
    print("ERR " + str(e)[:120]); raise SystemExit(3)
try:
    print("\n".join(d.find_orphan_scripts(Path("."))))
except Exception as e:
    print("ERR " + str(e)[:120]); raise SystemExit(3)
' 2>&1); ORC=$?

if [ "$ORC" -ne 0 ] || printf '%s' "$ORPHANS" | head -n1 | grep -q '^ERR '; then
  # Same rule as surface debt: a count this member cannot compute is never guessed at. This is
  # the exact failure maint_dead_code.py hid for 13 months -- it must read as LOUD, not as clean.
  report "QUIET — orphan scan could not run (rc=$ORC); nothing filed" \
         "$(printf '%s' "$ORPHANS" | tail -n 1 | cut -c1-200)" \
         "a scan that cannot look must never report clean -- that is the bug this axis exists to not repeat" "$ORC"
  echo "QUIET orphan scan rc=$ORC"
  exit 0
fi

ORPHAN_N=$(printf '%s' "$ORPHANS" | grep -c '[^[:space:]]' || true)
ANALYZER=$(cd "$REPO" && timeout 30s python3 -c '
import sys
sys.path.insert(0, "scripts")
import dead_code_lib as d
ok, which = d.analyzer_available()
print(("yes: " if ok else "NO -- unused-import/local scan did not run: ") + str(which))
' 2>/dev/null || echo "unknown")

if [ "${ORPHAN_N:-0}" -eq 0 ]; then
  report "QUIET — no orphan scripts; every tested script has a live caller" \
         "analyzer: $ANALYZER" "none -- deterministic over git ls-files" 0
  echo "QUIET orphans=0"
  exit 0
fi

# One dead-code retirement in flight at a time, same as surface debt.
DOPEN=$(cd "$REPO" && timeout 25s gh issue list --state open --search "in:title $DEAD_MARKER" --limit 5 \
  --json number --jq '.[0].number' 2>/dev/null || true)
if [ -n "$DOPEN" ] && [ "$DOPEN" != "null" ]; then
  report "QUIET — $ORPHAN_N orphan scripts but #$DOPEN is still open; one retirement at a time" \
         "analyzer: $ANALYZER | orphans: $ORPHAN_N" \
         "none -- a 42-item deletion epic is exactly the garbage nobody picks up" 0
  echo "QUIET orphans=$ORPHAN_N open=#$DOPEN"
  exit 0
fi

# Oldest-first: the longest-uncalled file is the least likely to be work in flight.
TARGET=$(cd "$REPO" && for f in $ORPHANS; do
    [ -f "$f" ] || continue
    printf '%s %s\n' "$(git log -1 --format=%ct -- "$f" 2>/dev/null || echo 0)" "$f"
  done | sort -n | head -n1 | cut -d" " -f2)

if [ -z "$TARGET" ]; then
  report "QUIET — $ORPHAN_N orphans but none resolved to a file on disk; nothing filed" \
         "analyzer: $ANALYZER" "the scan and the working tree disagree; not guessing" 1
  echo "QUIET no target"
  exit 0
fi

LASTTOUCH=$(cd "$REPO" && git log -1 --format=%as -- "$TARGET" 2>/dev/null || echo unknown)
DBODY=$(mktemp); trap 'rm -f "$BODY" "$DBODY"' EXIT
{
  echo "\`$TARGET\` has tests but **no non-test caller** anywhere in the repo."
  echo "Last touched: $LASTTOUCH. It is one of **$ORPHAN_N** such scripts right now."
  echo
  echo "## What to do"
  echo "Delete the script **and its test** — a test-only script is still dead, the test goes with it."
  echo "If it is NOT dead, do not delete it: add it to \`_KNOWN_EXTERNAL_ENTRYPOINTS\` in"
  echo "\`scripts/dead_code_lib.py\` with a one-line comment saying who calls it. Either outcome"
  echo "closes this item; both shrink the number."
  echo
  echo "## Before deleting, check the caller is not outside this repo"
  echo "This scan reads only this repo. A script called by a fleet member charter, a CI runner, or"
  echo "a runbook looks identical to a dead one. On 2026-09-12 the scan listed"
  echo "\`scripts/fleet/marie_collapse.py\` as an orphan in the same hour it was wired into marie's"
  echo "PART E — its caller lives in the fleet-kit repo. That one is now allowlisted; assume the"
  echo "next one could be the same and grep the fleet-kit repo before removing anything."
  echo
  echo "## Note on one-shot migrations"
  echo "Several orphans are \`backfill_*\` / \`fix_*\` scripts that ran once against prod and are"
  echo "correctly uncalled afterwards. Those are still dead code — delete them; git history keeps"
  echo "the record of what they did."
  echo
  echo "Analyzer status this pass: $ANALYZER"
  echo
  echo "---"
  echo "Filed by the \`custodian\` member (dead-code axis). The next pass files the next orphan"
  echo "only after this one lands, so the queue never grows a backlog nobody picks up."
} > "$DBODY"

DTITLE="$DEAD_MARKER: $TARGET has tests but no caller, retire it"
DNEW=$(cd "$REPO" && timeout 40s gh issue create --title "$DTITLE" --body-file "$DBODY" \
  --label fleet:backlog --label quality:solid 2>&1 | tail -n 1)
case "$DNEW" in
  http*)
    report "filed one dead-code retirement: $TARGET (of $ORPHAN_N orphans)" \
           "$DNEW | analyzer: $ANALYZER | last touched $LASTTOUCH" \
           "proposed, never auto-deleted -- an out-of-repo caller is invisible to this scan" 0
    echo "OK filed $DNEW"
    ;;
  *)
    report "QUIET — $ORPHAN_N orphans but filing failed; nothing was created" \
           "gh issue create said: $(printf '%s' "$DNEW" | cut -c1-200)" \
           "the next tick retries; no partial state is left behind" 1
    echo "QUIET dead-code file failed"
    ;;
esac
exit 0
