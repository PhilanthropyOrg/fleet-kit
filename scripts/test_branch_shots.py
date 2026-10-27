"""branch_shots.localize keeps the branch's CSS on the saved page (PR #14357's block)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import branch_shots  # noqa: E402


class LocalizeTest(unittest.TestCase):
    def test_static_links_point_at_saved_copies(self):
        html = '<link rel="stylesheet" href="/990/static/masthead.3d14d9d1.css"><img src="/990/static/a.svg">'
        out = branch_shots.localize(html)
        self.assertIn('href="990/static/masthead.3d14d9d1.css"', out)
        self.assertIn('src="990/static/a.svg"', out)
        self.assertNotIn('"/990/static/', out)

    def test_finds_every_static_url_once(self):
        html = '<a href="/990/static/x.css?v=1"></a><a href="/990/static/x.css?v=2"></a><a href="/other">'
        self.assertEqual(set(branch_shots.STATIC_RE.findall(html)), {"/990/static/x.css"})


if __name__ == "__main__":
    unittest.main()
