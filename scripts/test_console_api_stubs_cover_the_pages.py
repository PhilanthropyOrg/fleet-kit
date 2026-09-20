"""Reif 2026-09-19: "keep going with standardization -- so we don't revert back into oblivion."

The console's render tests share ONE stub table (console_api_stubs.ROUTES). This test fails
when a console page grows a fetch that the table does not know, which is the exact drift that
left /api/budget_preview and /api/minion_runs 404ing in four tests at once."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from console_api_stubs import ROUTES, STREAM_ROUTES  # noqa: E402

PAGES = ("fleet_home.html", "fleet_view.html")
FETCH_RE = re.compile(r"""['"`](/api/[a-z0-9_]+)""")


class StubsCoverThePages(unittest.TestCase):
    def test_every_api_path_a_console_page_fetches_has_a_stub(self):
        known = set(ROUTES) | set(STREAM_ROUTES)
        missing = {}
        for name in PAGES:
            src = (ROOT / "scripts" / name).read_text()
            for path in set(FETCH_RE.findall(src)) - known:
                missing.setdefault(path, []).append(name)
        self.assertEqual(missing, {},
                         "console pages fetch paths with no stub -- add them to "
                         "scripts/console_api_stubs.py ROUTES: " + repr(missing))

    def test_unknown_route_is_an_explicit_404_naming_the_fix(self):
        seen = {}

        class FakeReq:
            url = "http://x/fleet/philanthropy/api/nope"

        class FakeRoute:
            request = FakeReq()

            def fulfill(self, **kw):
                seen.update(kw)

        from console_api_stubs import boot_mocks
        boot_mocks(FakeRoute())
        self.assertEqual(seen.get("status"), 404)
        self.assertIn("console_api_stubs.py", seen["json"]["fix"])

    def test_path_prefix_is_stripped_so_prefixed_consoles_hit_the_same_stubs(self):
        from console_api_stubs import path_of

        class R:
            class request:
                url = "http://x/fleet/philanthropy/api/metrics?limit=1"

        self.assertEqual(path_of(R()), "/api/metrics")


if __name__ == "__main__":
    unittest.main()
