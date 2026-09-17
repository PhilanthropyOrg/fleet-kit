"""Regression test for fk#649's remaining scope: the messenger's morning brief needs a way to
see which issues are set to `quality:world-class`, since that's the only surface Reif has to
move the dial (docs/quality-standard.md section 0). Before this, `collect()` never queried for
the label at all.

Run: python3 scripts/test_messenger_brief.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import messenger_brief as mb  # noqa: E402


def _fake_sh(issues_by_slug):
    def _sh(cmd, timeout=60, cwd=None):
        if cmd[:3] == ["gh", "issue", "list"]:
            repo_i = cmd.index("--repo") + 1
            slug = cmd[repo_i]
            assert "--label" in cmd and cmd[cmd.index("--label") + 1] == mb.WORLD_CLASS_LABEL
            assert "--state" in cmd and cmd[cmd.index("--state") + 1] == "open"
            return json.dumps(issues_by_slug.get(slug, []))
        return ""
    return _sh


class WorldClassOpenTests(unittest.TestCase):
    def test_empty_when_no_issues_carry_the_label(self):
        with unittest.mock.patch.object(mb, "sh", side_effect=_fake_sh({})):
            self.assertEqual(mb.world_class_open(), [])

    def test_open_world_class_issues_are_returned_with_link_and_age(self):
        issues = {
            "The-Good-Project-Team/fleet-kit": [
                {"number": 42, "title": "Telegram-level thread view",
                 "url": "https://github.com/The-Good-Project-Team/fleet-kit/issues/42",
                 "createdAt": "2026-09-01T00:00:00Z"},
            ]
        }
        with unittest.mock.patch.object(mb, "sh", side_effect=_fake_sh(issues)):
            out = mb.world_class_open()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["number"], 42)
        self.assertEqual(out[0]["title"], "Telegram-level thread view")
        self.assertEqual(out[0]["url"], issues["The-Good-Project-Team/fleet-kit"][0]["url"])
        self.assertEqual(out[0]["created_at"], "2026-09-01T00:00:00Z")

    def test_malformed_gh_output_is_skipped_not_fatal(self):
        def _sh(cmd, timeout=60, cwd=None):
            return "not json"
        with unittest.mock.patch.object(mb, "sh", side_effect=_sh):
            self.assertEqual(mb.world_class_open(), [])


class CollectIncludesWorldClassOpenTests(unittest.TestCase):
    def test_collect_carries_the_world_class_open_key(self):
        sentinel = [{"repo": "x/y", "number": 7, "title": "t", "url": "u", "created_at": "c"}]
        with unittest.mock.patch.object(mb, "world_class_open", return_value=sentinel), \
             unittest.mock.patch.object(mb, "number_header", return_value=""), \
             unittest.mock.patch.object(mb, "gh_prs", return_value=[]), \
             unittest.mock.patch.object(mb, "asks_open", return_value=[]), \
             unittest.mock.patch.object(mb, "runs_since", return_value={"by_member": {}, "notable": []}), \
             unittest.mock.patch.object(mb, "deploys_since", return_value=[]), \
             unittest.mock.patch.object(mb, "plan_bets", return_value=""), \
             unittest.mock.patch.object(mb, "vision", return_value={}), \
             unittest.mock.patch.object(mb, "pages", return_value=[]):
            out = mb.collect(14)
        self.assertEqual(out["world_class_open"], sentinel)


class CollectIncludesCameInTests(unittest.TestCase):
    """fk#1129 slice 3: collect() wires came_in_since() through into the JSON the charter
    reads for the "Came in yesterday" section."""

    def test_collect_carries_the_came_in_key(self):
        sentinel = {"counts": {"alert": 3}, "summary": {"alert": "1 board item, 2 comments"},
                    "lines": {"alert": ["board item #412 created"]}, "dropped_total": 0}
        with unittest.mock.patch.object(mb, "came_in_since", return_value=sentinel), \
             unittest.mock.patch.object(mb, "world_class_open", return_value=[]), \
             unittest.mock.patch.object(mb, "number_header", return_value=""), \
             unittest.mock.patch.object(mb, "gh_prs", return_value=[]), \
             unittest.mock.patch.object(mb, "asks_open", return_value=[]), \
             unittest.mock.patch.object(mb, "runs_since", return_value={"by_member": {}, "notable": []}), \
             unittest.mock.patch.object(mb, "deploys_since", return_value=[]), \
             unittest.mock.patch.object(mb, "plan_bets", return_value=""), \
             unittest.mock.patch.object(mb, "vision", return_value={}), \
             unittest.mock.patch.object(mb, "pages", return_value=[]):
            out = mb.collect(14)
        self.assertEqual(out["came_in"], sentinel)

    def test_came_in_since_always_uses_a_24h_window_regardless_of_since_hours(self):
        """The brief's own since_hours (14 for morning, 6 for afternoon) must not shrink the
        intake window -- "Came in yesterday" always means a day."""
        seen = {}

        def fake_came_in(since_ts):
            seen["since_ts"] = since_ts
            return {}

        now = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)
        with unittest.mock.patch.object(mb.inbox, "came_in", side_effect=fake_came_in):
            mb.came_in_since(now)
        expected = (now - dt.timedelta(hours=24)).timestamp()
        self.assertAlmostEqual(seen["since_ts"], expected, delta=1)


class PlanBetsTests(unittest.TestCase):
    """fk#559 VP review round 1 fix 1 + fix 4: plan_bets() shares plan_rank's path resolver and
    names an absent plan explicitly instead of returning "" (which the charter would otherwise
    read as "brief broken", not "no plan yet")."""

    def test_no_plan_file_returns_explicit_marker_naming_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FLEET_REPO": tmp, "FLEET_INSTANCE_NAME": "no-such-instance"}
            with unittest.mock.patch.dict("os.environ", env, clear=False), \
                 unittest.mock.patch.object(mb, "_plan_blocking_note", return_value=""):
                out = mb.plan_bets()
        self.assertTrue(out.startswith("no plan file at "))
        self.assertTrue(out.endswith("no-such-instance.md yet"))

    def test_no_plan_file_appends_the_blocking_note_as_a_second_line(self):
        """fk#559 VP review round 2 fix 3: "no plan yet" alone leaves the morning block a dead
        end -- it must also say what would create the file and what it's waiting on."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FLEET_REPO": tmp, "FLEET_INSTANCE_NAME": "no-such-instance"}
            with unittest.mock.patch.dict("os.environ", env, clear=False), \
                 unittest.mock.patch.object(mb, "_plan_blocking_note",
                                             return_value="waiting on #570"):
                out = mb.plan_bets()
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("no plan file at "))
        self.assertEqual(lines[1], "waiting on #570")

    def test_plan_file_uses_the_same_path_plan_rank_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "docs" / "plan"
            plan_dir.mkdir(parents=True)
            (plan_dir / "an-instance.md").write_text("## Bets\n1. Do the thing -- #1\n")
            env = {"FLEET_REPO": tmp, "FLEET_INSTANCE_NAME": "an-instance-green"}
            with unittest.mock.patch.dict("os.environ", env, clear=False):
                out = mb.plan_bets()
        self.assertIn("Do the thing -- #1", out)


class PlanBlockingNoteTests(unittest.TestCase):
    """fk#559 VP review round 2 fix 3: the note names #570 and reflects its live label/state
    rather than a date that will go stale."""

    def _sh(self, state="OPEN", labels=()):
        def _fake(cmd, timeout=60, cwd=None):
            self.assertEqual(cmd[:3], ["gh", "issue", "view"])
            self.assertIn("570", cmd)
            return json.dumps({"state": state, "labels": [{"name": n} for n in labels]})
        return _fake

    def test_needs_human_op_names_it_explicitly(self):
        with unittest.mock.patch.object(mb, "sh", side_effect=self._sh(labels=["fleet:needs-human-op"])):
            out = mb._plan_blocking_note()
        self.assertIn("#570", out)
        self.assertIn("fleet:needs-human-op", out)

    def test_closed_issue_says_so(self):
        with unittest.mock.patch.object(mb, "sh", side_effect=self._sh(state="CLOSED")):
            out = mb._plan_blocking_note()
        self.assertIn("closed", out)

    def test_unreachable_gh_degrades_to_generic_line_not_an_exception(self):
        with unittest.mock.patch.object(mb, "sh", return_value=""):
            out = mb._plan_blocking_note()
        self.assertIn("#570", out)


if __name__ == "__main__":
    unittest.main()


class BriefIsWrittenNotMailedTest(unittest.TestCase):
    """Reif, 2026-09-15: prune all outgoing email. deliver() writes the brief and returns
    without touching Resend unless FLEET_BRIEF_EMAIL=1. Mutation: drop the gate and the
    first test reaches read_alert_env()/urlopen."""

    def test_default_writes_the_brief_and_does_not_mail(self):
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(mb, "LOG_DIR", Path(d)), \
             unittest.mock.patch.dict("os.environ", {"FLEET_BRIEF_EMAIL": ""}), \
             unittest.mock.patch.object(mb, "read_alert_env", side_effect=AssertionError("mail path reached")), \
             unittest.mock.patch("urllib.request.urlopen", side_effect=AssertionError("network reached")):
            rc, word = mb.deliver("morning", "# hello\n", want_pdf=False, force=True)
            self.assertEqual((rc, word), (0, "written-not-mailed"))
            written = list(Path(d).glob("brief-morning-*.md"))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].read_text(), "# hello\n")

    def test_opt_in_reaches_the_mail_path(self):
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(mb, "LOG_DIR", Path(d)), \
             unittest.mock.patch.dict("os.environ", {"FLEET_BRIEF_EMAIL": "1"}), \
             unittest.mock.patch.object(mb, "read_alert_env", return_value={}):
            rc, word = mb.deliver("morning", "# hello\n", want_pdf=False, force=True)
            self.assertEqual((rc, word), (1, "no-credentials"))
