"""One set of console API stubs, shared by every console render test.

Reif, 2026-09-19: "keep going with standardization -- so we don't revert back into oblivion."

Six test files each hand-rolled their own `if path == "/api/..."` ladder. When a console page
grew a fetch (`/api/budget_preview`, `/api/minion_runs`), some ladders learned it and some did
not, so the page 404'd in those tests only and the failure read as "the console is broken".
Four of those tests were also absent from ci.yml, so nobody saw it either way.

Add a route HERE when a page grows a fetch, and every render test learns it at once.

    from console_api_stubs import boot_mocks
    page.route("**/api/**", boot_mocks)          # or: boot_mocks(route, extra={...})
"""
from __future__ import annotations

from urllib.parse import urlparse

# path -> payload. A page that fetches something absent here gets an explicit 404 whose body
# names the route, so a drifted stub reads as drift and not as a product bug.
ROUTES: dict[str, dict] = {
    "/api/snapshot": {"runs": [], "members": [], "gh": {}, "as_of": 0},
    "/api/number": {"present": False, "payload": {}},
    "/api/fleet_state": {"enabled": True, "paused": False},
    "/api/members": {"members": []},
    "/api/spend": {"spend": []},
    "/api/kpi": {"kpi": []},
    "/api/plan": {"plan": []},
    "/api/next_fires": {"next_fires": []},
    "/api/metrics": {"metrics": [], "as_of": 0},
    "/api/minion_runs": {"runs": []},
    "/api/budget_preview": {"ok": True, "per_pass_usd": 0, "week": {}, "accounts": []},
    "/api/alerts": {"open": [], "recently_resolved": [], "counts": {},
                    "budget_safe": True, "worst": "ok"},
    "/api/pass_log": {"passes": []},
    "/api/stats": {"stats": {}},
    "/api/fleet_settings": {"settings": {}},
    "/api/priority_epic": {"epic": None},
    # Action endpoints. A render test must never actually fire one, but the page wires them at
    # load time and a 404 on wiring is indistinguishable from a broken console, so they answer
    # a bland ok. Keep them inert: no side effect is modelled here on purpose.
    "/api/fleet_toggle": {"ok": True, "enabled": True},
    "/api/run_now": {"ok": True, "started": None},
    "/api/build": {"ok": True},
    "/api/steer": {"ok": True},
    "/api/prune": {"ok": True, "removed": 0},
    "/api/login": {"ok": True},
    "/api/create_issue": {"ok": True, "number": 0},
    "/api/comment_issue": {"ok": True},
    "/api/close_issue": {"ok": True},
    "/api/close_pr": {"ok": True},
}

# Served as text/event-stream rather than JSON: an EventSource that gets JSON logs an error.
STREAM_ROUTES = ("/api/stream",)


def path_of(route) -> str:
    """The request path, with any console path prefix (/fleet/<instance>) stripped."""
    p = urlparse(route.request.url).path
    i = p.find("/api/")
    return p[i:] if i >= 0 else p


def boot_mocks(route, extra: dict | None = None) -> None:
    """Fulfil one console API request from ROUTES (plus `extra`, which wins)."""
    path = path_of(route)
    if path in STREAM_ROUTES:
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body="")
        return
    table = dict(ROUTES, **(extra or {}))
    if path in table:
        route.fulfill(json=table[path])
        return
    route.fulfill(status=404, json={"error": "unmocked route " + path,
                                    "fix": "add it to scripts/console_api_stubs.py ROUTES"})
