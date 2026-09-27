"""Log-stamp parsers read both clocks: old lines say UTC, lines written after the container
moved to America/Chicago say CDT/CST. Each must land on the same real instant -- a CDT stamp
read as UTC is five hours off, which misplaces status-page hours and KPI windows.

Run: python3 scripts/test_central_time.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# One instant, three spellings: 2026-07-01 17:00 UTC == 12:00 CDT; 2026-12-01 18:00 UTC == 12:00 CST.
SUMMER = dt.datetime(2026, 7, 1, 17, 0, tzinfo=dt.timezone.utc).timestamp()
WINTER = dt.datetime(2026, 12, 1, 18, 0, tzinfo=dt.timezone.utc).timestamp()


class LaneKpiStampTests(unittest.TestCase):
    def test_utc_cdt_and_cst_stamps_resolve_to_the_same_instant(self):
        import lane_kpi
        self.assertEqual(lane_kpi._line_epoch("[2026-07-01 17:00:00 UTC] deploy OK at abc"), SUMMER)
        self.assertEqual(lane_kpi._line_epoch("[2026-07-01 12:00:00 CDT] deploy OK at abc"), SUMMER)
        self.assertEqual(lane_kpi._line_epoch("[2026-12-01 12:00:00 CST] deploy OK at abc"), WINTER)


class StatusDataStampTests(unittest.TestCase):
    def test_a_cdt_line_lands_in_its_real_hour_not_the_mtime_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["FLEET_LOG_DIR"] = d
            import importlib
            import status_data
            importlib.reload(status_data)
            central = dt.timezone(dt.timedelta(hours=-5), "CDT")
            then = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)
            stamp = then.astimezone(central).strftime("%Y-%m-%d %H:%M:%S")
            p = Path(d) / "deploy_staleness_check.log"
            p.write_text(f"[deploy-staleness {stamp} CDT] STALE: behind origin/main\n")
            out, _ = status_data.read_component(["deploy_staleness_check.log"], hours=72)
            # Read as UTC (or dropped to the mtime fallback) it lands 5h (or 30h) off.
            self.assertEqual(out[72 - 1 - 30], status_data.BAD, out)
            self.assertEqual(out.count(status_data.BAD), 1, out)


class MessengerBriefDeployStampTests(unittest.TestCase):
    def test_deploy_lines_in_either_zone_are_compared_as_real_instants(self):
        import messenger_brief as mb
        with tempfile.TemporaryDirectory() as d:
            mb.LOG_DIR = Path(d)
            (Path(d) / "deploy.log").write_text(
                "[deploy 2026-07-01 16:30:00 UTC] DEPLOYED: old\n"   # 11:30 CDT -- before
                "[deploy 2026-07-01 12:30:00 CDT] DEPLOYED: new\n")  # 17:30 UTC -- after
            since = dt.datetime(2026, 7, 1, 17, 0, tzinfo=dt.timezone.utc)
            got = mb.deploys_since(since)
            self.assertEqual([ln.split("DEPLOYED: ")[1] for ln in got], ["new"], got)


if __name__ == "__main__":
    unittest.main()
