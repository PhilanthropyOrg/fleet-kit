#!/bin/bash
# jefe's llm.pregate (philanthropy#8215): jefe is the fleet's escalation desk and does nothing
# but its inbox, so an empty inbox is `green` and run_member.sh spawns no model -- $0 per quiet
# hour. FIRE when a member (or the fleet_msg.py watchdog) has sent jefe something.
exec python3 "$(dirname "$0")/../../scripts/fleet_msg.py" pregate --me jefe
