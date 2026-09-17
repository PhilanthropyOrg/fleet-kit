#!/usr/bin/env python3
"""member_launch.py -- the ONE way anything in this kit spawns a fleet member off-cron.

fk#1124: /api/run_now (fleet_view_server.py, dashboard-key-gated) and POST /webhook/run
(webhook_receiver.py, per-caller-token-gated, this issue) both need to fire a member right
now, in the background, and land the run on the same runs.jsonl feed a cron tick would. Before
this they would have been two independent Popen call sites with two chances to drift (member
name -> script resolution, FLEET_RUN_NOW, cwd, env) -- this is that one call site, imported by
both.

judge-judy is the one member that does NOT run through run_member.sh (it has its own
judge-judy.sh, predating member_spec.py's unification -- see run_member.sh's own header);
resolve_script() carries that special case so a caller never has to know about it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "scripts"))


def resolve_script(name: str) -> tuple[Path, list[str]]:
    """(script path, leading args) for `name` -- judge-judy's own .sh with no member-name
    arg, everything else run_member.sh with the member name as its first positional arg."""
    if name == "judge-judy":
        return KIT_DIR / "members" / "judge-judy" / "judge-judy.sh", []
    return KIT_DIR / "scripts" / "run_member.sh", [name]


def spawn(name: str, *, item: int | str | None = None, fired_by: str | None = None,
         reason: str | None = None, env_file: str | os.PathLike | None = None,
         cwd: str | os.PathLike | None = None) -> subprocess.Popen:
    """Popen one member in the background, detached, writing to the same runs.jsonl feed a
    cron tick uses. FLEET_RUN_NOW=1 (skips run_member.sh's own kill-switch checks, same as a
    dashboard click today -- an explicit call is a human/agent decision, not the thing those
    switches exist to gate). fired_by/reason (fk#1124) pass through as env vars; run_member.sh
    forwards them onto every record it writes for this run (run_report.py's --fired-by/
    --reason). Never blocks: the caller's HTTP request returns immediately, same as
    /api/run_now always has.
    """
    script, args = resolve_script(name)
    if item is not None:
        args = [*args, "--item", str(item)]
    env = dict(os.environ)
    env.setdefault("FLEET_ENV_FILE", str(env_file or (KIT_DIR / "fleet.env")))
    env["FLEET_RUN_NOW"] = "1"
    if fired_by:
        env["FLEET_FIRED_BY"] = fired_by
    if reason:
        # run_member.sh interpolates $REASON_FLAG unquoted (word-split) -- collapse
        # whitespace to underscores so a multi-word reason survives as one shell word,
        # same convention run_member.sh's own --items already uses for issue numbers.
        env["FLEET_FIRED_REASON"] = "_".join(reason.split())
    return subprocess.Popen(
        ["bash", str(script), *args],
        cwd=str(cwd) if cwd else str(KIT_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
