#!/usr/bin/env python3
"""audit_cleanup -- an auditor pass leaves no QA persona dirty and no fixture job open.

fk9: a scout-report pass signed in as the QA operator and never reset it. The charter now
records each persona and posted job as it goes, and runs `run` as the last step of the pass:
it POSTs /990/api/qa/reset for every recorded persona and checks every recorded job answers
410 (close it in HQ first). Anything left dirty prints LEFT and exits 1, so the Report says so.

State: $WT_PATH/audit-pass.json (else $FLEET_LOG_DIR), {"personas": [...], "jobs": [...]}.
Cleared only when every reset and every 410 check passed.

Usage: audit_cleanup.py persona <name> | job <url> | run
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

SITE = os.environ.get("AUDIT_SITE", "https://philanthropy.org")


def state_path() -> Path:
    base = os.environ.get("WT_PATH") or os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")
    return Path(base) / "audit-pass.json"


def load(path: Path) -> dict:
    try:
        s = json.loads(path.read_text())
    except (OSError, ValueError):
        s = {}
    return {"personas": list(s.get("personas", [])), "jobs": list(s.get("jobs", []))}


def record(path: Path, kind: str, value: str) -> None:
    s = load(path)
    if value not in s[kind]:
        s[kind].append(value)
    path.write_text(json.dumps(s))


def _http(method: str, url: str, headers: dict, body: bytes | None = None) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def cleanup(state: dict, env: dict, http=_http) -> list[str]:
    """One line per persona and job; a line starting LEFT is something still dirty."""
    out = []
    token, bypass = env.get("QA_SESSION_TOKEN", ""), env.get("ATLAS_TEST_BYPASS", "")
    base = {"x-atlas-test": bypass} if bypass else {}
    for p in state["personas"]:
        if not token:
            out.append(f"LEFT persona {p}: QA_SESSION_TOKEN is unset, could not reset")
            continue
        try:
            code = http("POST", f"{SITE}/990/api/qa/reset",
                        {**base, "x-qa-token": token, "Content-Type": "application/json"},
                        json.dumps({"user": p}).encode())
        except OSError as e:
            code = f"error {e}"
        out.append(f"reset persona {p}" if code == 200 else f"LEFT persona {p}: reset answered {code}")
    for url in state["jobs"]:
        try:
            code = http("GET", url, {**base, "User-Agent": "Mozilla/5.0 fleet-auditor"})
        except OSError as e:
            code = f"error {e}"
        out.append(f"closed job {url}" if code == 410 else
                   f"LEFT job {url}: answers {code}, not 410 (close it in HQ)")
    return out


def main(argv: list[str] | None = None, http=_http) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["persona", "job", "run"])
    ap.add_argument("value", nargs="?")
    ap.add_argument("--state", default="")
    a = ap.parse_args(argv)
    path = Path(a.state) if a.state else state_path()
    if a.cmd != "run":
        if not a.value:
            ap.error(f"{a.cmd} needs a value")
        record(path, a.cmd + "s", a.value)
        return 0
    lines = cleanup(load(path), dict(os.environ), http=http)
    print("\n".join(lines) or "nothing to clean: no persona or job recorded this pass")
    if any(ln.startswith("LEFT") for ln in lines):
        return 1
    path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
