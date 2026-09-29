"""A worker node adds CPU without adding a second claimer.

2026-09-29: dino ran at load 12-21 on 7 cores; lucky (6 cores) sat idle. A second full fleet on
lucky would run a second gru against the same backlog, and a claim is two non-atomic `gh` calls
(board_github.py: "NOT atomic -- GitHub has no test-and-set"). So the hub's gru keeps claiming;
dispatch_member.sh ships the claimed batch to the node over ssh, and the node's forced command
(node_gate.sh) can only run `minion --items <n,..>`. A batch runs in exactly one place: on the
node when it says node-accepted, otherwise on the hub.

RED without it: dispatch always runs run_member.sh locally, node_minion.sh / node_gate.sh do not
exist, and stale_claims.py does not see a node run as a live runner.

Run: python3 scripts/test_worker_node.py   (Linux: needs setsid + flock)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISPATCH = HERE / "dispatch_member.sh"
NODE_MINION = HERE / "node_minion.sh"
NODE_GATE = HERE / "node_gate.sh"
sys.path.insert(0, str(HERE))


def _sh(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.local = _sh(self.d / "run_member.sh", f'echo "local $@" >> {self.d}/ran\n')
        self.node = _sh(self.d / "node_minion.sh", f'echo "node $@" >> {self.d}/ran\n')
        (self.d / "nodecfg").mkdir()
        self.env = dict(os.environ, FLEET_RUN_MEMBER=str(self.local), FLEET_NODE_MINION=str(self.node),
                        FLEET_LOG_DIR=str(self.d), FLEET_NODE_DIR=str(self.d / "nodecfg"))

    def _ran(self, lines):
        f = self.d / "ran"
        deadline = time.time() + 10
        while time.time() < deadline and (not f.exists() or len(f.read_text().splitlines()) < lines):
            time.sleep(0.1)
        return f.read_text() if f.exists() else ""

    def test_minion_goes_to_node_when_configured(self):
        (self.d / "nodecfg" / "config").write_text("Host fleet-node\n")
        subprocess.run(["bash", str(DISPATCH), "minion", "--items", "11,12"], env=self.env, check=True)
        subprocess.run(["bash", str(DISPATCH), "nerd", "--task", "lane=claim x"], env=self.env, check=True)
        out = self._ran(2)
        self.assertIn("node minion --items 11,12", out)
        self.assertIn("local nerd --task lane=claim x", out, "only minions go to the node")

    def test_no_node_config_runs_here(self):
        subprocess.run(["bash", str(DISPATCH), "minion", "--items", "13"], env=self.env, check=True)
        self.assertIn("local minion --items 13", self._ran(1))


class NodeMinion(unittest.TestCase):
    """The hub side: run the batch on the node, or here only when the node never accepted it."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "nodecfg").mkdir()
        (self.d / "nodecfg" / "config").write_text("Host fleet-node\n")
        self.local = _sh(self.d / "run_member.sh", f'echo "local $@" >> {self.d}/ran\n')
        self.env = dict(os.environ, FLEET_RUN_MEMBER_LOCAL=str(self.local), FLEET_LOG_DIR=str(self.d),
                        FLEET_NODE_DIR=str(self.d / "nodecfg"))

    def _run(self, ssh_body: str, items="21,22"):
        ssh = _sh(self.d / "ssh", f'echo "$@" > {self.d}/ssh_args\n' + ssh_body)
        env = dict(self.env, FLEET_NODE_SSH_BIN=str(ssh))
        return subprocess.run(["bash", str(NODE_MINION), "minion", "--items", items], env=env,
                              capture_output=True, text=True, timeout=30)

    def _local_ran(self):
        f = self.d / "ran"
        return f.read_text() if f.exists() else ""

    def test_accepted_batch_relays_rows_and_never_runs_here(self):
        row = json.dumps({"run_id": "minion-item21_22-1-2", "status": "started"})
        r = self._run(f"echo 'node-accepted host=lucky'\necho 'node-row {row}'\necho 'node-done rc=0'\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._local_ran(), "")
        self.assertEqual((self.d / "runs.jsonl").read_text().strip(), row)
        self.assertIn("fleet-node minion --items 21,22", (self.d / "ssh_args").read_text())

    def test_refused_or_unreachable_runs_here(self):
        for body in ("echo 'node-refused: busy (3/3 slots)'\nexit 3\n", "exit 255\n"):
            (self.d / "ran").unlink(missing_ok=True)
            self._run(body)
            self.assertEqual(self._local_ran(), "local minion --items 21,22\n", body)

    def test_dropped_after_accept_is_not_run_twice(self):
        r = self._run("echo 'node-accepted host=lucky'\nexit 255\n")
        self.assertEqual(r.returncode, 255)
        self.assertEqual(self._local_ran(), "", "the node owns the batch once it accepted it")

    def test_bad_args(self):
        r = self._run("exit 0\n", items="1;id")
        self.assertEqual(r.returncode, 2)


class NodeGate(unittest.TestCase):
    """The node side: the hub's key can run a claimed minion batch and nothing else."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.logs = self.d / "logs"
        self.logs.mkdir()
        self.podman = _sh(self.d / "podman", f"""
echo "$@" >> {self.d}/podman_calls
case "$1" in
  inspect) cat {self.d}/running 2>/dev/null ;;
  exec) sleep 0.5
        echo '{{"run_id": "minion-item5_6-9-1", "status": "started"}}' >> {self.logs}/runs.jsonl
        echo '{{"run_id": "nerd-lane-x-9-1", "status": "started"}}' >> {self.logs}/runs.jsonl
        sleep 0.5
        echo '{{"run_id": "minion-item5_6-9-1", "status": "ok"}}' >> {self.logs}/runs.jsonl
        exit 0 ;;
esac
""")
        (self.d / "running").write_text("true\n")
        self.env = dict(os.environ, FLEET_NODE_ENV=str(self.d / "none"), FLEET_NODE_PODMAN=str(self.podman),
                        FLEET_NODE_LOG_DIR=str(self.logs), FLEET_NODE_SLOT_DIR=str(self.d / "slots"),
                        FLEET_NODE_CONTAINER="worker", FLEET_NODE_SLOTS="1", FLEET_NODE_POLL_S="0.2")

    def _gate(self, cmd: str):
        return subprocess.run(["bash", str(NODE_GATE)], env=dict(self.env, SSH_ORIGINAL_COMMAND=cmd),
                              capture_output=True, text=True, timeout=30)

    def _podman_calls(self):
        f = self.d / "podman_calls"
        return f.read_text() if f.exists() else ""

    def test_only_minion_batches(self):
        for cmd in ("", "gru", "bash", "minion --items", "minion --items 1;id", "minion --items 1 && id",
                    "minion --items 1,2 --task x", "minion --items $(id)", "nerd --task lane=x"):
            r = self._gate(cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertTrue(r.stdout.startswith("node-refused"), cmd)
        self.assertEqual(self._podman_calls(), "", "a refused command must never reach podman")

    def test_runs_batch_and_relays_only_its_rows(self):
        r = self._gate("minion --items 5,6")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith("node-accepted"), lines)
        rows = [json.loads(x[len("node-row "):]) for x in lines if x.startswith("node-row ")]
        self.assertEqual([x["status"] for x in rows], ["started", "ok"])
        self.assertEqual(lines[-1], "node-done rc=0")
        self.assertIn("exec -e FLEET_RUN_NOW=1 worker bash /fleet-kit/scripts/run_member.sh minion --items 5,6",
                      self._podman_calls())

    def test_busy_node_refuses(self):
        (self.d / "slots").mkdir()
        holder = subprocess.Popen(["flock", str(self.d / "slots" / "slot1"), "sleep", "30"])
        try:
            time.sleep(0.3)
            r = self._gate("minion --items 5")
            self.assertEqual(r.returncode, 3)
            self.assertIn("node-refused: busy", r.stdout)
            self.assertNotIn("exec", self._podman_calls())
        finally:
            holder.kill()

    def test_stopped_container_refuses(self):
        (self.d / "running").write_text("false\n")
        r = self._gate("minion --items 5")
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("exec", self._podman_calls())


class HubSideLiveness(unittest.TestCase):
    def test_stale_claims_sees_a_node_run_as_live(self):
        import stale_claims
        argv = ["bash", "/fleet-kit/scripts/node_minion.sh", "minion", "--items", "7,8"]
        self.assertEqual(stale_claims.numbers_in_argv(argv), {7, 8})

    def test_worker_entrypoint_schedules_nothing(self):
        ep = (HERE.parent / "entrypoint.sh").read_text()
        worker = ep.index('if [ "${FLEET_NODE_ROLE:-}" = "worker" ]; then')
        self.assertLess(worker, ep.index("scripts/fleet_view_server.py"))
        self.assertLess(worker, ep.index('CRONTAB=/etc/cron.d/fleet-kit'))
        self.assertIn("exec sleep infinity", ep[worker:worker + 400])


if __name__ == "__main__":
    unittest.main()
