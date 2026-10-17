#!/usr/bin/env python3
"""audit_scoreboard -- holes found by the auditor vs by hand (Viken, Reif) this week. (a1)

Target: Viken finds 0 new ones. Viken's count = rows in viken-status.md (`| V<id> | ...`)
that are still a hole (status not LIVE and not context only). The auditor's count = issues
labelled lane:audit opened in the last 7 days. Hand-found Jobs holes = issues opened in the
last 7 days whose title starts "Jobs" and that do NOT carry lane:audit.

Usage: audit_scoreboard.py [--repo OWNER/REPO, default: gh repo view in /repo] [--viken PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_file import repo_slug  # noqa: E402

SHARES = Path("/fleet-kit/shares/g")
_ROW = re.compile(r"^\|\s*(V\d+[a-z]?)\s*\|\s*([^|]*)\|\s*([^|]*)\|", re.M)


def viken_holes(text: str) -> list[tuple[str, str]]:
    """(id, ask) for each Viken row whose status is still a hole."""
    out = []
    for vid, ask, status in _ROW.findall(text):
        s = status.strip().upper()
        if s.startswith("LIVE") or s.startswith("CONTEXT"):
            continue
        out.append((vid, ask.strip()))
    return out


def split_issues(issues: list[dict], since: dt.datetime) -> tuple[list[dict], list[dict]]:
    """(auditor-found, hand-found Jobs) among issues created on or after `since`."""
    audit, hand = [], []
    for i in issues:
        made = dt.datetime.fromisoformat(i["createdAt"].replace("Z", "+00:00"))
        if made < since:
            continue
        labels = {lab["name"] for lab in i.get("labels", [])}
        if "lane:audit" in labels:
            audit.append(i)
        elif i["title"].startswith("Jobs"):
            hand.append(i)
    return audit, hand


def render(viken: list, audit: list, hand: list, now: dt.datetime) -> str:
    lines = [f"# Audit scoreboard (week to {now:%Y-%m-%d %H:%M} UTC)", "",
             "| Found by | Holes |", "|---|---|",
             f"| Auditor (lane:audit) | {len(audit)} |",
             f"| Viken (open rows in viken-status.md) | {len(viken)} |",
             f"| By hand on Jobs (not lane:audit) | {len(hand)} |", "",
             "Target: Viken finds 0 new ones.", ""]
    lines += ["## Auditor"] + [f"- #{i['number']} {i['title']}" for i in audit] + [""]
    lines += ["## Viken"] + [f"- {v}: {a}" for v, a in viken] + [""]
    lines += ["## By hand on Jobs"] + [f"- #{i['number']} {i['title']}" for i in hand]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="")
    ap.add_argument("--viken", default=str(SHARES / "viken-status.md"))
    ap.add_argument("--out", default=str(SHARES / "audit-scoreboard.md"))
    a = ap.parse_args(argv)
    a.repo = repo_slug(a.repo)
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=7)
    viken = viken_holes(Path(a.viken).read_text()) if Path(a.viken).exists() else []
    issues = json.loads(subprocess.run(
        ["gh", "issue", "list", "--repo", a.repo, "--state", "all", "--limit", "500",
         "--search", f"created:>={since:%Y-%m-%d}", "--json", "number,title,labels,createdAt"],
        capture_output=True, text=True, check=True).stdout)
    audit, hand = split_issues(issues, since)
    Path(a.out).write_text(render(viken, audit, hand, now))
    print(f"wrote {a.out}: auditor {len(audit)}, Viken {len(viken)}, by hand {len(hand)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
