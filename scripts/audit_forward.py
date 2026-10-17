#!/usr/bin/env python3
"""audit_forward -- each new hole pattern the auditors file reaches dumbledore, once.

Reif, 2026-10-17: holes surface themselves. The auditors (fk#1687) file `lane:audit` issues,
one per kind of mistake, keyed by an `Audit-pattern: <key>` line. A pattern means some lane
(auditor, sentry, a persona walk) let a hole through; dumbledore fixes that lane. jefe runs
`send` as a step of its pass; its pregate runs `pending` so a new pattern wakes it.

State: $FLEET_LOG_DIR/audit_forwarded.json, the pattern keys already sent. fleet_msg dedupes
only 6h, so without this a pattern open for a week would be sent 28 times.

Usage: audit_forward.py pending | send   [--repo OWNER/REPO, default: gh repo view in /repo]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import audit_file as af  # noqa: E402


def state_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "audit_forwarded.json"


def patterns(issues: list[dict]) -> dict[str, dict]:
    """pattern key -> the oldest open issue carrying it."""
    out: dict[str, dict] = {}
    for i in sorted(issues, key=lambda i: i["number"]):
        m = re.search(r"^Audit-pattern: (\S+)", i.get("body") or "", re.M)
        if m:
            out.setdefault(m.group(1), i)
    return out


def new_patterns(issues: list[dict], sent: set[str]) -> dict[str, dict]:
    return {k: i for k, i in patterns(issues).items() if k not in sent}


def load_sent() -> set[str]:
    try:
        return set(json.loads(state_path().read_text()))
    except (OSError, ValueError):
        return set()


def open_audit_issues(repo: str) -> list[dict]:
    return json.loads(subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--label", "lane:audit", "--state", "open",
         "--limit", "200", "--json", "number,title,body"],
        capture_output=True, text=True, check=True, timeout=60).stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pending", "send"])
    ap.add_argument("--repo", default="", help="OWNER/REPO; default: gh repo view in /repo")
    a = ap.parse_args(argv)
    a.repo = af.repo_slug(a.repo)
    sent = load_sent()
    new = new_patterns(open_audit_issues(a.repo), sent)
    if a.cmd == "pending":
        print(f"FIRE {len(new)} new audit pattern(s)" if new else "green no new audit pattern")
        return 0
    for key, i in new.items():
        body = (f"Audit pattern `{key}` (#{i['number']} {i['title']}). A lane let this hole "
                "through: name it (auditor, sentry or persona walk), fix its charter, add a "
                "predict row.")
        subprocess.run(["python3", str(HERE / "fleet_msg.py"), "send", "--from", "jefe",
                        "--to", "dumbledore", "--kind", "cause", "--key", f"audit:{key}",
                        "--body", body], check=True, timeout=60)
        sent.add(key)
        state_path().write_text(json.dumps(sorted(sent)))
        print(f"forwarded {key} (#{i['number']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
