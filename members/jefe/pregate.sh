#!/bin/bash
# jefe's llm.pregate (philanthropy#8215): jefe is the fleet's escalation desk and does nothing
# but its inbox, so an empty inbox is `green` and run_member.sh spawns no model -- $0 per quiet
# hour. FIRE when a member (or the fleet_msg.py watchdog) has sent jefe something, or when an
# auditor filed a hole pattern dumbledore has not had yet (audit_forward.py).
S="$(dirname "$0")/../../scripts"
out=$(python3 "$S/fleet_msg.py" pregate --me jefe)
case "$out" in green*) audit=$(python3 "$S/audit_forward.py" pending 2>/dev/null) && case "$audit" in FIRE*) out="$audit";; esac;; esac
echo "$out"
