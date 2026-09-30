#!/usr/bin/env python3
"""fleet_chat_act.py -- the one command a dashboard chat pass may run (see fleet_chat.py).

    python3 fleet_chat_act.py get  /api/members
    python3 fleet_chat_act.py post /api/members/nerd/pause '{}'

Calls this box's own dashboard over localhost. Every POST carries X-Fleet-On-Behalf with the
person who asked (FLEET_CHAT_WHO, set by fleet_chat.py, not by the model), so writes.jsonl
records "<name> (chat)" rather than an anonymous localhost write.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("get", "post") or not argv[1].startswith("/api/"):
        print("usage: fleet_chat_act.py get|post /api/<route> ['<json body>']", file=sys.stderr)
        return 2
    verb, route = argv[0], argv[1]
    url = f"http://127.0.0.1:{os.environ.get('FLEET_VIEW_PORT', '8420')}{route}"
    data = None
    headers = {}
    if verb == "post":
        body = argv[2] if len(argv) > 2 else "{}"
        try:
            json.loads(body)
        except ValueError:
            print("body must be JSON", file=sys.stderr)
            return 2
        data = body.encode()
        headers = {"Content-Type": "application/json",
                   "X-Fleet-On-Behalf": os.environ.get("FLEET_CHAT_WHO") or "unknown"}
    req = urllib.request.Request(url, data=data, headers=headers, method=verb.upper())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        out = e.read().decode(errors="replace")
        print(f"HTTP {e.code}", file=sys.stderr)
    # Big reads (snapshot is ~570KB) would flood the pass's context; the model can narrow.
    print(out[:60000] + ("\n...[cut at 60000 chars; use a narrower route]" if len(out) > 60000 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
