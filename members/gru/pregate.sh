#!/bin/bash
# gru's llm.pregate (MANDATE 2026-10-08, "max CI mins used today 500"). Once the day's Actions
# minutes are spent, run_member.sh refuses every minion and every the-fixer --item push (fk#1609,
# fk#1625), so a gru pass can only report "no build": 10 such passes cost $3.2 from 20:06Z to
# 23:05Z on 10-08 (2858..3024/500) and dispatched fixers the gate then turned away. Green = spent
# day and no open ask: $0, no model. Inbox mail still wakes gru (run_member.sh checks it on green).
# Fails open: an unread budget or ask list proceeds to the model, as before.
# Only an ask gru may answer wakes it (gru.md step 10): a money/credential ask waits on Reif, so
# ask #138 (money) woke ~10 hourly "no build" passes on 10-09 08:00-14:06Z, $0.33 each.
KIT="$(dirname "$0")/../.."
BUDGET="$(python3 "$KIT/scripts/build_lane_rule.py" ci-budget 2>/dev/null)"
[ $? -eq 3 ] || { echo "FIRE ci-budget ${BUDGET:-unread}"; exit 0; }
ASKS="$(python3 "$KIT/scripts/ask.py" list --status open 2>/dev/null | python3 -c '
import json, sys
print(len([a for a in json.load(sys.stdin) if a.get("class") not in ("money", "credential")]))' 2>/dev/null)"
[ "$ASKS" = "0" ] || { echo "FIRE open asks gru can answer: ${ASKS:-unread} (ci-budget $BUDGET)"; exit 0; }
echo "green -- CI day spent ($BUDGET), no ask gru can answer; nothing gru dispatches can run before 00:00 UTC"
