#!/usr/bin/env python3
"""denial_asks.py -- a member's `gh` permission denial becomes an ask, never a silent stop.

philanthropy#8215, 2026-09-26: dont-shoot-the-messenger (20:08 UTC) and sentry (19:02 UTC) each
had `gh issue create` denied by their sandbox. Both passes stopped politely and said so in a
report nobody reads; a real org's removal request sat unfiled until a human noticed.

run_member.sh pipes each pass's final `result` event (stream-json, carries
`permission_denials: [{tool_name, tool_use_id, tool_input}]`) into this script after the pass.
Every denied Bash call that runs `gh` becomes ONE ask for that pass (no page: the next brief and
the console's asks carry it), unless an open ask from the same member already names the same
gh verb -- a member denied every hour asks once, not 24 times. Dedupe redirects from
issue_create_hook.py (an open twin, not a permission) are skipped by tool_use_id.

Best-effort by design: anything unreadable exits 0 and never fails the pass.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

GH_VERB = re.compile(r"(?:^|[\s;&|(])gh\s+([a-z-]+)(?:\s+([a-z-]+))?")
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
TAG = "permission denied:"


def gh_denials(result: dict, skip_ids: set[str] | None = None) -> list[dict]:
    """[{"verb": "gh issue create", "command": ...}] for each denied Bash call that runs gh."""
    out = []
    for d in result.get("permission_denials") or []:
        if d.get("tool_name") != "Bash" or d.get("tool_use_id") in (skip_ids or set()):
            continue
        cmd = (d.get("tool_input") or {}).get("command") or ""
        m = GH_VERB.search(cmd)
        if not m:
            continue
        verb = " ".join(x for x in ("gh", m.group(1), m.group(2)) if x)
        out.append({"verb": verb, "command": cmd})
    return out


def redirect_ids(path: Path | None = None) -> set[str]:
    try:
        lines = (path or LOG_DIR / "dedupe_redirects.jsonl").read_text().splitlines()[-500:]
    except OSError:
        return set()
    ids = set()
    for line in lines:
        try:
            ids.add(json.loads(line).get("tool_use_id"))
        except ValueError:
            pass
    return {i for i in ids if i}


def plan_ask(member: str, run_id: str, denials: list[dict], open_asks: list[dict]) -> dict | None:
    """The one ask this pass files, or None (nothing denied, or already asked)."""
    already = {a.get("why", "") for a in open_asks if a.get("member") == member}
    fresh, seen = [], set()
    for d in denials:
        head = f"{TAG} {d['verb']}"
        if d["verb"] in seen or any(w.startswith(head) for w in already):
            continue
        seen.add(d["verb"])
        fresh.append(d)
    if not fresh:
        return None
    verbs = ", ".join(d["verb"] for d in fresh)
    sample = fresh[0]["command"].replace("\n", " ")[:240]
    return {
        "why": (f"{TAG} {verbs} -- {member}'s sandbox refused it in pass {run_id}, so whatever "
                f"that call was for did not happen. First refused command: {sample}"),
        "summary": f"{member} was blocked from a GitHub action it needed ({verbs})",
        "unblocks": f"{member} can finish that step on its next pass",
        "proposed": (f"allow it in members/{member}/{member}.fleet.json (tools.deny), or say the "
                     f"deny is right and {member}'s charter should route around it"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--member", required=True)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        result = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    denials = gh_denials(result if isinstance(result, dict) else {}, redirect_ids())
    if not denials:
        return 0
    try:
        import ask as ask_mod
        import fleet_db
        conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
        # philanthropy#8215 amendment: any member -> jefe on a permission denial, deduped per
        # (verbs) for 6h by fleet_msg itself. Sent even when the ask already exists: the ask is
        # Reif's, the message is jefe's to answer (keep the deny, or allow it).
        if not a.dry_run:
            import fleet_msg
            verbs = sorted({d["verb"] for d in denials})
            fleet_msg.send(conn, a.member, ["jefe"], "permission-denial",
                           f"{a.member}:{','.join(verbs)}",
                           f"{a.member}'s sandbox refused {', '.join(verbs)} in pass {a.run_id}. "
                           f"First refused command: {denials[0]['command'][:240]}")
        p = plan_ask(a.member, a.run_id, denials, ask_mod.list_asks(conn, status="open", limit=500))
        if not p:
            print(f"denial_asks: {len(denials)} gh denial(s), already asked")
            return 0
        if a.dry_run:
            print(json.dumps(p))
            return 0
        ask_id = ask_mod.file_ask(conn, a.member, p["why"], p["unblocks"], p["proposed"],
                                  ask_class="infra", summary=p["summary"])
        ask_mod.route_to_triage(conn, ask_id, a.member, p["why"], "infra", wake=False)  # dumbledore triages it
        print(f"denial_asks: ask {ask_id} filed for {len(denials)} gh denial(s)")
    except Exception as exc:  # noqa: BLE001 -- never fail the pass over its own bookkeeping
        print(f"denial_asks: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
