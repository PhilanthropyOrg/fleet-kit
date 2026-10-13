#!/bin/bash
# dumbledore's llm.pregate: green (no model, $0) when three bets are open, one of them a
# ladder bet, no verdict landed and no ask waits -- the pass could only write
# `Prediction: none`. Unacked INBOX mail still wakes it (run_member.sh). See the script.
exec python3 "$(dirname "$0")/../../scripts/dumbledore_pregate.py"
