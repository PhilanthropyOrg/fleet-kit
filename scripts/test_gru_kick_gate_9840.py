#!/usr/bin/env python3
"""gh#9840: run_gru_fanout.sh exits before any sweep when gru is running or a deploy kick is
under 45 min after the last start. Run: python3 scripts/test_gru_kick_gate_9840.py"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "run_gru_fanout.sh"


def run(tmp: str, fired_by: str = "") -> str:
    env = dict(os.environ, TMPDIR=tmp, FLEET_ENV_FILE="/nonexistent", FLEET_FIRED_BY=fired_by)
    # a stub PATH python3 would hide real work; the gate must exit before any python3 call
    env["PATH"] = tmp + ":" + env["PATH"]
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return r.stdout + r.stderr


def stub_python(tmp: str) -> Path:
    p = Path(tmp) / "python3"
    p.write_text("#!/bin/sh\necho SWEEP_RAN\nexit 1\n")
    p.chmod(0o755)
    return p


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        locks = Path(tmp) / "fleet-kit-member-locks"
        locks.mkdir()
        stub_python(tmp)
        # 1. lock held -> exits, no sweep
        holder = subprocess.Popen(["flock", str(locks / "gru.lock"), "sleep", "3"])
        time.sleep(0.5)
        out = run(tmp)
        holder.wait()  # the sleep child holds the lock until it exits
        assert "already running" in out and "SWEEP_RAN" not in out, out
        # 2. fresh stamp + deploy kick -> skipped
        (locks / "gru.last-start").touch()
        out = run(tmp, "deploy_kick")
        assert "deploy kick skipped" in out and "SWEEP_RAN" not in out, out
        # 3. old stamp + deploy kick -> proceeds to the sweeps
        old = time.time() - 3600
        os.utime(locks / "gru.last-start", (old, old))
        out = run(tmp, "deploy_kick")
        assert "SWEEP_RAN" in out, out
        # 4. fresh stamp, cron (not a kick) -> proceeds
        (locks / "gru.last-start").touch()
        out = run(tmp)
        assert "SWEEP_RAN" in out, out
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
