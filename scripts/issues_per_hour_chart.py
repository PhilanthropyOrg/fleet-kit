#!/usr/bin/env python3
"""issues_per_hour_chart -- the full-width 7-day graph on fleet_home, built on top of
scoreboard.resolved_events (#1279's already-merged, already-deployed scoring: merged PR closes
an issue COMPLETED, carried live by a later successful deploy; NOT_PLANNED never counts; a
fleet:mega counts its full folded-children checklist).

#1279 shipped the SCORE (small metric tiles, 24h value + sparkline). This module is UI-only on
top of it: Reif's own full-width graph spec -- hourly bars for the last 7 days, stacked DIRECT
vs MEGA-CHILDREN credit (not one blended weight), 24h and 7d rolling averages as lines, and
closed-without-PR as a separate MUTED series so a burst of manual/dupe/stale closes can never be
mistaken for real throughput. No new scoring logic lives here; `resolved_events`'s own
`is_mega` flag (added alongside this module) is the only extension to #1279's engine.

The SVG is built server-side, not in fleet_home.html's own JS, because that page carries a hard
byte cap (selftest.py: `len(page.encode()) < 48_000`, fk#645 "simplify down, way down") that
already had almost no headroom before this feature.
"""
from __future__ import annotations

import datetime


def hour_bucket(epoch: float) -> str:
    """UTC hour bucket key, e.g. '2026-09-24T13:00Z' -- one string per hour, sortable as text."""
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:00Z")


def hourly_series(events: list[dict], closed_without_pr_events: list[dict], hours: int = 24 * 7,
                   now: float | None = None) -> dict:
    """Bucket #1279's `resolved_events` (plus a list of closed-without-PR {"ts"} rows) into the
    last `hours` UTC hours (default 7 days), split direct vs mega-child (via each event's own
    `is_mega`), stacked with a separate closed-without-PR series, plus rolling 24h/7d averages
    of the credited total (direct + mega_child) per hour."""
    now = now if now is not None else datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
    end_hour = int(now // 3600) * 3600
    buckets = [end_hour - i * 3600 for i in range(hours - 1, -1, -1)]
    keys = [hour_bucket(b) for b in buckets]
    direct = {k: 0 for k in keys}
    mega_child = {k: 0 for k in keys}
    no_pr = {k: 0 for k in keys}
    for e in events:
        k = hour_bucket(e["ts"])
        if k in direct:
            (mega_child if e.get("is_mega") else direct)[k] += e["weight"]
    for e in closed_without_pr_events:
        k = hour_bucket(e["ts"])
        if k in no_pr:
            no_pr[k] += 1
    totals = [direct[k] + mega_child[k] for k in keys]

    def avg_over(n_hours: int, idx: int) -> float:
        lo = max(0, idx - n_hours + 1)
        window = totals[lo:idx + 1]
        return round(sum(window) / len(window), 3) if window else 0.0

    return {
        "hours": keys,
        "direct": [direct[k] for k in keys],
        "mega_child": [mega_child[k] for k in keys],
        "closed_without_pr": [no_pr[k] for k in keys],
        "avg_24h": [avg_over(24, i) for i in range(len(keys))],
        "avg_7d": [avg_over(24 * 7, i) for i in range(len(keys))],
        "current_24h_rate": avg_over(24, len(keys) - 1),
    }


def chart_svg(series: dict, w: int = 1200, h: int = 160) -> str:
    """The full-width stacked-bar chart as a ready-to-inject SVG string. Colors are CSS custom
    properties (var(--accent)/var(--ok)/var(--faint)/var(--ink-2)) so it follows the page's own
    light/dark theme even though it's generated server-side."""
    hours = series["hours"]
    n = len(hours)
    if not n:
        return ""
    pad, pb = 5, 4
    ph = h - pad - pb
    bw = (w - 2 * pad) / n
    hi = max([1] + [series["direct"][i] + series["mega_child"][i] + series["closed_without_pr"][i]
                    for i in range(n)] + series["avg_24h"] + series["avg_7d"])

    def sy(v: float) -> float:
        return pad + ph - v / hi * ph

    def sx(i: int) -> float:
        return pad + i * bw

    bars = []
    for i in range(n):
        top = pad + ph
        for color, v in (("var(--faint)", series["closed_without_pr"][i]),
                         ("var(--ok)", series["mega_child"][i]),
                         ("var(--accent)", series["direct"][i])):
            if not v:
                continue
            bh = v / hi * ph
            top -= bh
            bars.append(f'<rect fill="{color}" x="{sx(i) + bw * .08:.1f}" y="{top:.1f}" '
                        f'width="{bw * .84:.1f}" height="{bh:.1f}"/>')

    def points(arr: list[float]) -> str:
        return " ".join(f"{sx(i) + bw / 2:.1f},{sy(v):.1f}" for i, v in enumerate(arr))

    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display:block;width:100%;height:{h}px">'
            + "".join(bars)
            + f'<polyline fill="none" stroke="var(--faint)" stroke-dasharray="3 3" points="{points(series["avg_7d"])}"/>'
            + f'<polyline fill="none" stroke="var(--ink-2)" points="{points(series["avg_24h"])}"/>'
            + "</svg>")
