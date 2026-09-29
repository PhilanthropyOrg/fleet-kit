#!/usr/bin/env python3
"""philanthropy#8714: auto_deploy.sh keeps the HOST checkout on main every tick (even inside the
gh#619 deploy-coalescing window), records what it deployed in the #8703 box-log area, and pages
through fleet_alert.sh when it cannot deploy.

Runs the REAL auto_deploy.sh against a git fixture with stubbed deploy.sh / fleet_alert.sh and
the real webhook_receiver.py (for record_box_logs) -- same harness shape as selftest.py's gh#619
coalescing check.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin = tmp / "origin.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        seed = tmp / "seed"
        seed.mkdir()
        for cmd in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(seed, *cmd)
        git(seed, "remote", "add", "origin", str(origin))
        (seed / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "auto_deploy.sh", seed / "scripts" / "auto_deploy.sh")
        shutil.copy(ROOT / "scripts" / "webhook_receiver.py", seed / "scripts" / "webhook_receiver.py")
        (seed / "scripts" / "deploy.sh").write_text(
            '#!/bin/bash\n[ -f "${DEPLOY_STUB_FAIL:-/nonexistent}" ] && exit 1\necho x >> "${DEPLOY_STUB_MARKER:?}"\n')
        (seed / "scripts" / "fleet_alert.sh").write_text('#!/bin/bash\necho "$*" >> "${ALERT_STUB_LOG:?}"\n')
        (seed / ".gitignore").write_text("__pycache__/\n")
        (seed / "foo.txt").write_text("v1\n")
        git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "init"); git(seed, "push", "-q", "origin", "main")

        checkout = tmp / "host"
        git(tmp, "clone", "-q", str(origin), str(checkout))
        instance = tmp / "instance"
        (instance / "logs").mkdir(parents=True)
        (instance / "fleet.env").write_text("")
        marker, alerts, fail = tmp / "deploys", tmp / "alerts", tmp / "fail"
        box_logs = instance / "logs" / "box-logs.jsonl"

        def tick():
            env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
            env.update(HOME=str(tmp / "home"), FLEET_LOG_DIR=str(tmp / "logs"), FLEET_CONTAINER_NAME="test",
                       FLEET_INSTANCE_DIR=str(instance), DEPLOY_STUB_MARKER=str(marker),
                       ALERT_STUB_LOG=str(alerts), DEPLOY_STUB_FAIL=str(fail))
            return subprocess.run(["bash", "scripts/auto_deploy.sh"], cwd=checkout, env=env,
                                  capture_output=True, text=True, timeout=60)

        def deploys():
            return len(marker.read_text().splitlines()) if marker.exists() else 0

        def shipped():
            return [json.loads(l)["msg"] for l in box_logs.read_text().splitlines()] if box_logs.exists() else []

        def push(n):
            (seed / "foo.txt").write_text(f"v{n}\n"); git(seed, "commit", "-aq", "-m", f"move {n}"); git(seed, "push", "-q", "origin", "main")
            return git(seed, "rev-parse", "HEAD")

        # 1. First move deploys and records the deployed SHA in the one log area.
        sha2 = push(2)
        assert tick().returncode == 0
        assert deploys() == 1, "first move did not deploy"
        assert any(f"deployed {sha2}" in m for m in shipped()), f"deployed SHA not in box-logs: {shipped()}"

        # 2. Move inside the coalescing window: container deploy deferred, HOST checkout still
        #    fast-forwarded to main (host-side crons run from it), and the deferral is recorded.
        sha3 = push(3)
        assert tick().returncode == 0
        assert deploys() == 1, "a move inside the gh#619 window deployed anyway"
        assert git(checkout, "rev-parse", "HEAD") == sha3, "host checkout not fast-forwarded inside the coalescing window"
        assert any(sha3[:7] in m and "deploy window" in m for m in shipped()), f"deferral not recorded: {shipped()}"

        # 3. A failed deploy pages through fleet_alert.sh (once the window opens).
        stamp = tmp / "home" / ".cache" / "fleet-kit" / "auto_deploy.last_sha.test.deployed_at"
        stamp.write_text(str(int(time.time()) - 7201) + "\n")
        fail.touch()
        assert tick().returncode == 1
        assert "--problem deploy_failed" in (alerts.read_text() if alerts.exists() else ""), "failed deploy did not page"
        fail.unlink()
        assert tick().returncode == 0 and deploys() == 2, "retry after a failed deploy did not deploy"
        assert "--resolve --check auto_deploy" in alerts.read_text(), "successful deploy did not resolve the alert"

        # 4. Dirty host checkout: refuses to pull (never forces) and pages.
        (checkout / "foo.txt").write_text("hand patch\n")
        push(4)
        assert tick().returncode == 1
        assert (checkout / "foo.txt").read_text() == "hand patch\n", "local change was overwritten"
        assert "--problem dirty_tree" in alerts.read_text(), "dirty checkout did not page"
    print("ok test_auto_deploy_8714")


if __name__ == "__main__":
    main()
