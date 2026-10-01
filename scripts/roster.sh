#!/bin/bash
# roster.sh -- who is in the fleet, read from the specs, so no doc has to keep a table of it.
#
# Prints one row per members/<name>/<name>.fleet.json: on/off, schedule, model, and the
# member's own one-line job (`plain`). Live overrides (scripts/overrides.py -- a dashboard
# throttle that needs no PR) are applied when their store is readable, and each one is marked
# with "*"; the last lines say whether the store was found. On a laptop it is not, so the
# table there is the reviewed baseline only. For the live view run it inside the container:
#   podman exec -e FLEET_LOG_DIR=/var/log/fleet-kit <container> bash /fleet-kit/scripts/roster.sh
#
# The spec's schedule is the reviewed intent. The cron lines the container really runs are
# rendered by entrypoint.sh (fleet.env can narrow or retime them); see the footer.
#
# Usage: bash scripts/roster.sh [--json]
set -eu
cd "$(dirname "$0")"
exec python3 - "${1:-}" <<'PY'
import json
import sys

import member_spec
import overrides


def when(s):
    if "hourly_at_minute" in s:
        return "hourly :%02d" % s["hourly_at_minute"]
    if "daily_at" in s:
        return "daily " + s["daily_at"]
    n = s["interval_s"]
    return "every %gh" % (n / 3600) if n >= 3600 else "every %g min" % (n / 60)


rows = []
for spec in member_spec.load_all():
    eff, applied = overrides.apply(spec)
    rows.append({
        "name": eff["name"],
        "enabled": bool(eff.get("enabled", True)),
        "schedule": eff["schedule"],
        "model": eff.get("llm", {}).get("model", ""),
        "job": eff.get("plain", ""),
        "overridden": sorted(r["key"] for r in applied),
    })

store = overrides.STORE
if sys.argv[1] == "--json":
    print(json.dumps({"members": rows, "overrides_store": str(store),
                      "overrides_store_found": store.exists()}, indent=2))
    sys.exit(0)

print("| member | on | schedule (spec) | model | job |")
print("|---|---|---|---|---|")
for r in rows:
    o = r["overridden"]
    print("| %s | %s%s | %s%s | %s%s | %s |" % (
        r["name"],
        "yes" if r["enabled"] else "no", "*" if "enabled" in o else "",
        when(r["schedule"]), "*" if "schedule" in o else "",
        r["model"] or "-", "*" if "model" in o else "",
        r["job"]))
print()
print("on=no: cron never starts it by itself; another member still can (gru spawns its builders).")
print("Real cron lines: podman exec <container> cat /etc/cron.d/fleet-kit  (entrypoint.sh renders them).")
if store.exists():
    print("Live overrides: applied from %s (* marks an overridden value)." % store)
else:
    print("Live overrides: NOT shown -- no store at %s. This is the spec baseline." % store)
PY
