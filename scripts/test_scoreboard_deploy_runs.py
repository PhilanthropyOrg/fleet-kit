""""Issues resolved" read 0 on 2026-09-30 while 56 issues closed by merged, deployed PRs.

RED before this change: _scoreboard_deploy_runs asked only `gh run list --status success`,
which GitHub serves stale (newest run 09-29 08:14 while fresh successes existed at 17:39), so
no merge after the cut-off had a covering deploy and every such fix counted as not live.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
(TMP / "fleet.env").write_text("FLEET_DEPLOY_WORKFLOW=DEPLOY\n")
os.environ.update(FLEET_ENV_FILE=str(TMP / "fleet.env"), FLEET_LOG_DIR=str(TMP), FLEET_REPO=str(TMP))
sys.path.insert(0, str(KIT / "scripts"))
import fleet_view_server as fvs  # noqa: E402
import scoreboard  # noqa: E402

STALE = [{"createdAt": "2026-09-29T08:14:25Z", "conclusion": "success", "headSha": "a"},
         {"createdAt": "2026-09-20T08:00:00Z", "conclusion": "success", "headSha": "old"}]
FRESH = [{"createdAt": "2026-09-30T17:52:07Z", "conclusion": "", "headSha": "c"},
         {"createdAt": "2026-09-30T17:39:34Z", "conclusion": "success", "headSha": "b"},
         {"createdAt": "2026-09-30T17:11:34Z", "conclusion": "cancelled", "headSha": "x"},
         {"createdAt": "2026-09-29T08:14:25Z", "conclusion": "success", "headSha": "a"}]


def fake_gh(*args, timeout=15):
    assert args[:4] == ("run", "list", "--workflow", "DEPLOY"), args
    return json.dumps(STALE if "--status" in args else FRESH)


class DeployRuns(unittest.TestCase):
    def setUp(self):
        fvs._TTL_CACHE.clear()
        self._gh, fvs._gh = fvs._gh, fake_gh

    def tearDown(self):
        fvs._gh = self._gh
        fvs._TTL_CACHE.clear()

    def test_fresh_successes_are_kept_with_the_history(self):
        got = sorted(r["createdAt"] for r in fvs._scoreboard_deploy_runs())
        self.assertEqual(got, ["2026-09-20T08:00:00Z", "2026-09-29T08:14:25Z", "2026-09-30T17:39:34Z"])

    def test_a_fix_merged_today_counts_once_deployed(self):
        pr = {"number": 1, "mergedAt": "2026-09-30T15:00:00Z", "closingIssuesReferences": [{"number": 9}]}
        issue = {"number": 9, "state": "CLOSED", "stateReason": "COMPLETED", "labels": [], "body": "",
                 "comments": [], "closedAt": "2026-09-30T15:00:00Z"}
        ev = scoreboard.resolved_events([pr], fvs._scoreboard_deploy_runs(), {9: issue})
        self.assertEqual([e["issue"] for e in ev], [9])


if __name__ == "__main__":
    unittest.main()
