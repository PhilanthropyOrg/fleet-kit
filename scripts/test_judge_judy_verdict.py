"""judge_judy_verdict.py: every finding carries a plain-English sentence (Reif, 2026-09-12:
"I want all reports to be written in plain english"), and the rendered text leads with it."""
from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge_judy_verdict as jv  # noqa: E402


def _envelope(obj):
    return json.dumps({"structured_output": obj})


class PlainFinding(unittest.TestCase):
    def test_finding_without_plain_is_rejected(self):
        r = jv.parse(_envelope({"verdict": "block", "findings": [
            {"file": "a.py", "line": 3, "severity": "high", "what_breaks": "x() returns None"}]}))
        self.assertFalse(r["ok"], r)
        self.assertIn("plain", r["reason"])

    def test_findings_text_leads_with_the_plain_sentence(self):
        r = jv.parse(_envelope({"verdict": "block", "findings": [
            {"file": "a.py", "line": 3, "severity": "high", "what_breaks": "x() returns None",
             "plain": "Signing in would fail for everyone."}]}))
        self.assertTrue(r["ok"], r)
        first = r["findings_text"].splitlines()[0]
        self.assertIn("Signing in would fail for everyone.", first)
        self.assertNotIn("a.py", first)
        self.assertIn("a.py:3", r["findings_text"])

    def test_approve_with_no_findings_still_ok(self):
        self.assertTrue(jv.parse(_envelope({"verdict": "approve", "findings": []}))["ok"])


class RetriedAccount(unittest.TestCase):
    """The pool's 429 envelope, then the second account's real answer, on one stdout."""

    def test_the_answer_after_a_rate_limit_envelope_is_read(self):
        limited = json.dumps({"is_error": True, "api_error_status": 429,
                              "result": "You've hit your weekly limit"})
        raw = limited + "\n" + _envelope({"verdict": "approve", "findings": []}) + "\n"
        r = jv.parse(raw)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["verdict"], "approve")

    def test_garbage_still_strikes(self):
        self.assertFalse(jv.parse('{"structured_output": {}}\nnot json')["ok"])


import value_judge as vj


def _scored(verdict="approve", v=2, u=2, f=2, b=2, research=True, findings=None,
            who=("a donor reading a 990 report", "report checkouts a week")):
    obj = {"verdict": verdict, "findings": findings or [],
           "beneficiary": {"user": who[0], "number": who[1]},
           "scores": {r: {"score": n, "why": r + " why"} for r, n in
                      zip(vj.RISKS, (v, u, f, b))},
           "research": {"ok": research, "why": "named Zillow, Candid"}}
    return jv.parse(_envelope(obj))


class ValueJudge(unittest.TestCase):
    """PHIA-1451: the four big risks decide the send-back and the points, in code."""

    def test_scores_pass_through_the_verdict_parser(self):
        r = _scored()
        self.assertEqual(r["scores"]["value"]["score"], 2)
        self.assertTrue(r["research"]["ok"])

    def test_points_are_value_times_the_mean_of_the_rest(self):
        self.assertEqual(vj.points({"value": 3, "usability": 3, "feasibility": 2, "viability": 1}), 6.0)

    def test_a_valuable_researched_pr_passes(self):
        out = vj.apply(_scored(), "", "1", "abc", "t", "jobs/x", False)
        self.assertEqual(out["verdict"], "approve")
        self.assertEqual(out["ledger"]["points"], 4.0)
        self.assertEqual(out["ledger"]["seat"], "v-jobs")  # PHIA-2089: one name per seat

    def test_each_send_back_rule(self):
        card = {"task": "PHIA-9", "owner": "jobs", "card": "User: a donor\nBest in class: ?", "plan": ""}
        for kw, word in (({"u": 0}, "Usability is 0"),
                         ({"v": 2, "u": 1, "f": 1, "b": 1}, "Too little value")):
            out = vj.apply(_scored(**kw), "", "1", "abc", "t", "g/x", False, card)
            # Reif 11-02 (judge before build): a value rule above the floor is a note, never a send-back
            self.assertEqual(out["verdict"], "approve", kw)
            self.assertIn(word, out["findings_text"], kw)
            self.assertIn("**Value notes**", out["findings_text"], kw)
            self.assertEqual(out["ledger"]["value_reasons"], [], kw)
        for kw in ({"v": 0}, {"v": 1}, {"research": False}):  # below the floor: blocks (Paul 11-02)
            out = vj.apply(_scored(**kw), "", "1", "abc", "t", "g/x", False, card)
            self.assertEqual(out["verdict"], "block", kw)
            self.assertIn("Value floor", out["findings_text"], kw)
        for kw, word in (({"v": 0}, "Value is 0"), ({"u": 0}, "Usability is 0"),
                         ({"research": False}, "value card on PHIA-9 is thin"),
                         ({"v": 1}, "Value is 1"),
                         ({"v": 2, "u": 1, "f": 1, "b": 1}, "Too little value")):
            # the undo switch puts the old send-back back
            out = vj.apply(_scored(**kw), "", "1", "abc", "t", "g/x", False, card, value_send_back=True)
            self.assertEqual(out["verdict"], "block", kw)
            self.assertIn("**Sent back:**", out["findings_text"], kw)

    def test_no_named_user_and_number_caps_value_at_1_and_sends_back(self):
        # Paul 11-01: 45 of 51 verdicts were 2/2/2/2 and 0 went back for value.
        for who in (("", "checkouts"), ("a donor", "none"), ("n/a", "")):
            out = vj.apply(_scored(v=3, who=who), "", "1", "h", "t", "g/x", False, value_send_back=True)
            self.assertEqual(out["verdict"], "block", who)
            self.assertEqual(out["ledger"]["scores"]["value"], 1)
            self.assertIn("capped at 1", out["scores_text"])
        out = vj.apply(_scored(v=3), "", "1", "h", "t", "g/x", False)
        self.assertEqual(out["verdict"], "approve")
        self.assertIn("**Who gets it:** a donor reading a 990 report", out["scores_text"])
        self.assertEqual(out["ledger"]["number"], "report checkouts a week")

    def test_value_2_needs_a_measured_reading_of_the_number(self):
        # Paul 11-01 06:11Z: 5 of 8 verdicts still 2/2/2/2; a 2 on Value needs a measured number.
        for baseline in ("", "none", "goes up"):
            obj = _scored(v=2)
            obj["beneficiary"]["baseline"] = baseline
            out = vj.apply(obj, "", "1", "h", "t", "g/x", False, value_send_back=True)
            self.assertEqual(out["verdict"], "block", baseline)
            self.assertEqual(out["ledger"]["scores"]["value"], 1)
            self.assertIn("no measured reading", out["scores_text"])
        obj = _scored(v=2)
        obj["beneficiary"]["baseline"] = "jobs/applies 41 a day, kpi_daily 10-31"
        out = vj.apply(obj, "", "1", "h", "t", "g/x", False)
        self.assertEqual(out["verdict"], "approve")
        self.assertIn("(today: jobs/applies 41 a day", out["scores_text"])
        self.assertEqual(out["ledger"]["baseline"], "jobs/applies 41 a day, kpi_daily 10-31")
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        self.assertIn("Value 2 or 3 needs a named user and a measured baseline", sh)

    def test_half_paid_at_merge_half_owed_until_the_read_and_a_cut_in_full(self):
        # PHIA-1918: the second half waits for check48 on the named metric.
        obj = _scored(v=3, u=3, f=2, b=1)
        obj["beneficiary"].update(metric="jobs/applies", guess=4, cut=False)
        led = vj.apply(obj, "", "1", "h", "t", "jobs/x", False)["ledger"]
        self.assertEqual((led["points"], led["pay_at_merge"], led["pay_on_read"]), (6.0, 3.0, 3.0))
        self.assertEqual((led["metric"], led["guess"], led["cut"]), ("jobs/applies", 4.0, False))
        obj["beneficiary"]["cut"] = True
        out = vj.apply(obj, "", "1", "h", "t", "jobs/x", False)
        self.assertEqual((out["ledger"]["pay_at_merge"], out["ledger"]["pay_on_read"]), (6.0, 0.0))
        self.assertIn("paid in full at merge", out["scores_text"])
        back = vj.apply(_scored(v=0), "", "1", "h", "t", "jobs/x", False)["ledger"]
        self.assertEqual((back["pay_at_merge"], back["pay_on_read"]), (0.0, 0.0))
        old = vj.apply(_scored(), "", "1", "h", "t", "jobs/x", False)["ledger"]  # verdict with no metric fields
        self.assertEqual((old["metric"], old["guess"], old["pay_at_merge"]), ("", 0.0, 2.0))

    def test_judge_before_build_task_gate(self):
        # Reif 11-02: a PR passes the task check only when its task carries judge-yes or fast-lane.
        new = "2026-11-03T10:00:00Z"
        def card(labels, created=new, **kw):
            return dict({"task": "PHIA-9", "owner": "v-jobs", "card": "User: x", "plan": "",
                         "labels": labels, "created_at": created}, **kw)
        for labels in (["judge-yes"], ["fast-lane"], [{"id": "l1", "name": "judge-yes"}]):
            out = vj.apply(_scored(), "", "1", "h", "PHIA-9 x", "jobs/x", False, card(labels), gate=True)
            self.assertEqual(out["verdict"], "approve", labels)
        out = vj.apply(_scored(), "", "1", "h", "PHIA-9 x", "jobs/x", False, card([]), gate=True)
        self.assertEqual(out["verdict"], "block")
        self.assertIn("no Judge YES", out["findings_text"])
        self.assertEqual(out["ledger"]["approval_reasons"][0][:10], "PHIA-9 has")
        self.assertEqual((out["ledger"]["pay_at_merge"], out["ledger"]["pay_on_read"]), (0.0, 0.0))
        # a correctness pass on a low-value PR with an approved task still ships (value is not re-judged)
        out = vj.apply(_scored(v=1), "", "1", "h", "PHIA-9 x", "jobs/x", False, card(["judge-yes"]), gate=True)
        self.assertEqual(out["verdict"], "approve")
        # a correctness block still blocks
        bad = [{"file": "a.py", "line": 3, "severity": "high", "what_breaks": "x() returns None", "plain": "Sign-in fails."}]
        out = vj.apply(_scored(verdict="block", findings=bad), "", "1", "h", "PHIA-9 x", "jobs/x", False,
                       card(["judge-yes"]), gate=True)
        self.assertEqual(out["verdict"], "block")
        # tasks from before the cutover are not held to it
        out = vj.apply(_scored(), "", "1", "h", "PHIA-9 x", "jobs/x", False, card([], "2026-11-01T00:00:00Z"), gate=True)
        self.assertEqual(out["verdict"], "approve")
        # no task named at all
        out = vj.apply(_scored(), "", "1", "h", "fix it", "jobs/x", False, {}, gate=True)
        self.assertEqual(out["verdict"], "block")
        self.assertIn("names no task", out["findings_text"])
        # the PR names a task but the board read failed or timed out ({} from judge-judy): never fatal
        for c in ({}, {"task": "PHIA-9", "error": "TimeoutError"}, {"task": "PHIA-9", "card": "x"}):
            out = vj.apply(_scored(), "", "1", "h", "PHIA-9 x", "jobs/x", False, c, gate=True)
            self.assertEqual(out["verdict"], "approve", c)
        # dependabot (exempt) and the gate off pass
        self.assertEqual(vj.apply(_scored(), "", "1", "h", "bump", "dependabot/x", True, {}, gate=True)["verdict"], "approve")
        self.assertEqual(vj.apply(_scored(), "", "1", "h", "fix it", "jobs/x", False, {}, gate=False)["verdict"], "approve")

    def test_value_card_reads_labels_and_age(self):
        import value_card as vc
        from unittest import mock

        def _open(req, timeout=None):
            if "/documents/" in req.full_url:
                return io.StringIO(json.dumps({"body": "User: a donor", "updatedAt": "t"}))
            return io.StringIO(json.dumps({"title": "Pitch", "description": "", "createdAt": "2026-11-03T00:00:00Z",
                                           "labels": [{"id": "1", "name": "judge-yes"}]}))
        with mock.patch.dict(os.environ, {"PAPERCLIP_API_URL": "http://b", "PAPERCLIP_API_KEY": "k"}), \
                mock.patch.object(vc.urllib.request, "urlopen", _open):
            out = vc.fetch("PHIA-77")
        self.assertEqual((out["labels"], out["created_at"]), (["judge-yes"], "2026-11-03T00:00:00Z"))

    def test_a_measuring_tool_caps_at_value_2(self):
        # G 11-01 (PHIA-2043): tools scored on the visitor number they gate, never Value 3.
        obj = _scored(v=3, u=2, f=2, b=2)
        obj["beneficiary"].update(metric="jobs/applies", guess=1, cut=False, tool=True)
        out = vj.apply(obj, "", "247", "h", "t", "automation/x", False)
        self.assertEqual(out["ledger"]["scores"]["value"], 2)
        self.assertTrue(out["ledger"]["tool"])
        self.assertIn("capped at 2", out["scores_text"])
        self.assertEqual(out["verdict"], "approve")
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        self.assertIn("measuring tools (G, PHIA-2043)", sh)
        self.assertIn("Owner-asked cost (G, PHIA-2067 and PHIA-2070)", sh)
        self.assertIn("platform/ci_usd_per_day", sh)

    def test_task_description_stands_in_for_a_missing_card(self):
        import value_card as vc
        from unittest import mock

        def _open(req, timeout=None):
            if "/documents/" in req.full_url:
                raise vc.urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            return io.StringIO(json.dumps({"title": "Fix the PF report", "description":
                                           "Donors see 0 gifts.\nSelf-score: Value 3, Usability 2, Feasibility 2, Viability 2"}))
        with mock.patch.dict(os.environ, {"PAPERCLIP_API_URL": "http://b", "PAPERCLIP_API_KEY": "k"}), \
                mock.patch.object(vc.urllib.request, "urlopen", _open):
            out = vc.fetch("PHIA-1")
        self.assertEqual(out["card"], "")
        self.assertIn("Donors see 0 gifts", out["task_text"])
        self.assertNotIn("Value 3", out["task_text"])
        self.assertIn("Value 3", out["plan"])
        res = vj.apply(_scored(), "", "1", "h", "t", "g/x", False, out)
        self.assertEqual(res["verdict"], "approve")  # a missing card is never a block on its own
        self.assertIn("| Research (task as card) |", res["scores_text"])
        res = vj.apply(_scored(research=False), "", "1", "h", "t", "g/x", False, out)
        self.assertEqual(res["verdict"], "block")  # but no research is under the value floor (Paul 11-02)
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        self.assertIn(".task_text", sh)
        for anchor in ("Anchors", "14985", "14835", "what would make it a 3"):
            self.assertIn(anchor, sh)

    def test_no_research_section_in_the_body_is_not_what_counts(self):
        # Paul 10-31: 10 of 10 vertical PRs had no ## Research; research is judged from the card, not the body.
        out = vj.apply(_scored(research=True), "## Why\nx", "1", "h", "t", "g/x", False)
        self.assertEqual(out["verdict"], "approve")
        out = vj.apply(_scored(research=False), "## Research\nlots", "1", "h", "t", "g/x", False,
                       {"task": "PHIA-9", "owner": "g", "card": "", "plan": ""})
        self.assertEqual(out["verdict"], "block")

    def test_value_floor_fails_a_1_of_3_card_with_no_research(self):
        # Paul 11-02 (Paperclip 2): #15261 passed with Value 1/3 and no research. The real card, as judged.
        obj = _scored(v=1, u=2, f=2, b=2, research=False,
                      who=("A donor or researcher reading an org's insider dealings", "990/profile_views"))
        obj["research"]["why"] = "The task card has no Best in class line and no Data reading."
        card = {"task": "PHIA-2317", "owner": "platform", "card": "", "plan": "",
                "task_text": "one-shot box cron runs the Schedule L backfill", "labels": [],
                "created_at": "2026-11-02T10:00:00Z"}
        out = vj.apply(obj, "", "15261", "7d4e507", "PHIA-2317: one-shot box cron", "phia2317-x", False, card,
                       gate=True)
        self.assertEqual(out["verdict"], "block")  # the model said PASS on correctness; the floor overrules it
        self.assertEqual(len(out["ledger"]["floor_reasons"]), 2)
        self.assertIn("Value is 1/3, the floor is 2", out["findings_text"])
        self.assertIn("no research on PHIA-2317", out["findings_text"])
        self.assertEqual(out["ledger"]["pay_at_merge"], 0.0)
        self.assertEqual(out["scores_text"].count("Value is 1"), 1)  # said once, by the floor
        # a 2+ card with research clears it on the same task
        ok = vj.apply(_scored(v=2, research=True), "", "15261", "h", "PHIA-2317: x", "phia2317-x", False, card, gate=True)
        self.assertEqual((ok["verdict"], ok["ledger"]["floor_reasons"]), ("approve", []))
        # the floor holds whether or not the model blocked; judge-yes / fast-lane tasks were floored on the pitch
        for labels in (["judge-yes"], ["fast-lane"]):
            out = vj.apply(obj, "", "15261", "h", "PHIA-2317: x", "phia2317-x", False, dict(card, labels=labels), gate=True)
            self.assertEqual(out["verdict"], "approve", labels)
        # exempt (dependabot) and the undo switch pass
        self.assertEqual(vj.apply(obj, "", "1", "h", "bump", "dependabot/x", True, card)["verdict"], "approve")
        self.assertEqual(vj.apply(obj, "", "1", "h", "t", "x/y", False, card, value_floor=False)["verdict"], "approve")

    def test_flat_judge_is_flagged(self):
        # Paul 11-02: in one hour 5 of 8 reviews scored exactly 2/2/2/2.
        def row(pr, s, ts, **kw):
            return dict({"pr": str(pr), "ts": ts, "scores": dict(zip(vj.RISKS, s)), "exempt": False}, **kw)
        flat8 = [row(i, (2, 2, 2, 2), i) for i in range(5)] + [row(10 + i, (1, 2, 2, 3), 10 + i) for i in range(3)]
        got = vj.flat(flat8, window=8, share=0.6)
        self.assertTrue(got["flat"])
        self.assertEqual((got["scores"], got["count"], got["reviews"]), ("2/2/2/2", 5, 8))
        spread = [row(i, (i % 4, 2, (i + 1) % 4, 3), i) for i in range(8)]
        self.assertFalse(vj.flat(spread, window=8, share=0.6)["flat"])
        # too few reviews to say; a re-review of one PR counts once; exempt rows are left out
        self.assertFalse(vj.flat(flat8[:4], window=8)["flat"])
        rereview = [row(1, (2, 2, 2, 2), t) for t in range(8)]
        self.assertFalse(vj.flat(rereview, window=8)["flat"])
        self.assertFalse(vj.flat([dict(r, exempt=True) for r in flat8], window=8)["flat"])
        # the CLI reads the ledger file
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(json.dumps(r) for r in flat8) + "\n")
        try:
            buf = io.StringIO()
            with unittest.mock.patch.object(sys, "stdout", buf):
                self.assertEqual(vj.main(["flat", f.name]), 0)
            self.assertTrue(json.loads(buf.getvalue())["flat"])
        finally:
            os.unlink(f.name)

    def test_card_owner_is_the_seat_and_its_self_score_is_logged_blind(self):
        card = {"task": "PHIA-1217", "owner": "atlas", "card": "User: x",
                "plan": "Self-score: Value 2, Usability 2, Feasibility 2, Viability 2"}
        out = vj.apply(_scored(), "", "1", "h", "Quadrant words", "phia1217-quadrant-words", False, card)
        self.assertEqual(out["ledger"]["seat"], "atlas")
        self.assertEqual(out["ledger"]["task"], "PHIA-1217")
        self.assertTrue(out["ledger"]["plan_matched"])
        self.assertTrue(out["ledger"]["card"])

    def test_one_name_per_seat_from_branch_prefix_or_card(self):  # PHIA-2089
        for ref in ("auto/x", "automation/x", "f-automation/x"):
            self.assertEqual(vj.seat_of(ref), "platform")
        self.assertEqual(vj.seat_of("jobs/x"), "v-jobs")
        self.assertEqual(vj.seat_of("ops/x"), "platform")
        self.assertEqual(vj.seat_of("g/x"), "g")
        self.assertEqual(vj.seat_of("no-slash"), "unknown")
        out = vj.apply(_scored(), "", "1", "h", "t", "jobs/x", False, {"owner": "Claims", "card": ""})
        self.assertEqual(out["ledger"]["seat"], "v-claims-and-superpages")
        rows = [{"ts": 100.0, "pr": "1", "seat": "auto", "verdict": "approve", "points": 2.0},
                {"ts": 100.0, "pr": "2", "seat": "f-automation", "verdict": "approve", "points": 3.0}]
        seats = {d["seat"] for d in vj.summary(rows, now=200.0)["points_per_seat_per_day"]}
        self.assertEqual(seats, {"platform"})

    def test_value_card_cuts_the_self_score_before_the_judge_sees_it(self):
        import value_card as vc
        card, plan = vc.blind("User: a donor\nNeed: x\n4-risk self-score: Value 3, Usability 2, Feasibility 2, Viability 1\nBest in class: Candid")
        self.assertNotIn("Value 3", card)
        self.assertIn("Best in class: Candid", card)
        self.assertEqual(vj.plan_score("plan " + plan)["value"], 3)
        self.assertEqual(vc.fetch("not-a-task"), {})

    def test_value_card_sends_the_board_host_header(self):
        import value_card as vc
        from unittest import mock
        seen = []

        class _R(io.StringIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _open(req, timeout=None):
            seen.append((req.full_url, req.get_header("Host"), req.get_header("Authorization")))
            return _R("{}")
        env = {"PAPERCLIP_API_URL": "http://172.18.0.3:3100", "PAPERCLIP_API_KEY": "k",
               "PAPERCLIP_HOST": "localhost:3100"}
        with mock.patch.dict(os.environ, env), mock.patch.object(vc.urllib.request, "urlopen", _open):
            self.assertEqual(vc._get("/api/issues/PHIA-1"), ({}, ""))
            del os.environ["PAPERCLIP_HOST"]
            vc._get("/api/issues/PHIA-1")
        self.assertEqual(seen[0], ("http://172.18.0.3:3100/api/issues/PHIA-1", "localhost:3100", "Bearer k"))
        self.assertIsNone(seen[1][1])

    def test_a_card_read_that_fails_says_why_not_no_card(self):
        # PHIA-1504: #14830 had a card, but the table said "no card" -- a failed read looked like none.
        import value_card as vc
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            out = vc.fetch("PHIA-1")
        self.assertEqual(out["task"], "PHIA-1")
        self.assertIn("not in the judge's environment", out["error"])

        def _404(req, timeout=None):
            if "/documents/" in req.full_url:
                raise vc.urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            return io.StringIO("{}")
        env = {"PAPERCLIP_API_URL": "http://b", "PAPERCLIP_API_KEY": "k"}
        with mock.patch.dict(os.environ, env), mock.patch.object(vc.urllib.request, "urlopen", _404):
            out = vc.fetch("PHIA-1")
        self.assertEqual(out["card"], "")
        self.assertNotIn("error", out)
        text = vj.apply(_scored(), "", "1", "h", "t", "g/x", False, out)["scores_text"]
        self.assertIn("| Research (no card) |", text)
        text = vj.apply(_scored(), "", "1", "h", "t", "g/x", False,
                        {"task": "PHIA-1", "error": "HTTP 403"})["scores_text"]
        self.assertIn("| Research (card unread: HTTP 403) |", text)
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        self.assertIn("export PAPERCLIP_API_URL PAPERCLIP_API_KEY PAPERCLIP_HOST", sh)

    def test_card_read_waits_past_a_slow_board_and_reads_issue_and_card_at_once(self):
        # PHIA-1504: board reads ran p95 ~105 s; at 20 s, 7 of 13 verdicts said "card unread: TimeoutError".
        import threading
        import value_card as vc
        from unittest import mock
        timeouts, inside, peak, lock = [], [0], [0], threading.Lock()

        class _R(io.StringIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _slow(req, timeout=None):
            timeouts.append(timeout)
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            import time
            time.sleep(0.2)
            with lock:
                inside[0] -= 1
            if "/documents/" in req.full_url:
                return _R(json.dumps({"body": "User: a donor", "updatedAt": "2026-10-31T19:20:14Z"}))
            return _R("{}")
        env = {"PAPERCLIP_API_URL": "http://b", "PAPERCLIP_API_KEY": "k"}
        with mock.patch.dict(os.environ, env), mock.patch.object(vc.urllib.request, "urlopen", _slow):
            out = vc.fetch("PHIA-1")
        self.assertEqual(out["card"], "User: a donor")
        self.assertNotIn("error", out)
        self.assertGreaterEqual(min(timeouts), 120)
        self.assertEqual(peak[0], 2)

    def test_a_timed_out_card_read_retries_then_uses_the_saved_read(self):
        # Paul 11-01 06:11Z: 5 of 8 verdicts said "card unread: TimeoutError" at 150 s (PHIA-1929).
        import tempfile
        import value_card as vc
        from unittest import mock
        calls, fail = [], [False]

        class _R(io.StringIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _open(req, timeout=None):
            calls.append(req.full_url)
            if fail[0]:
                raise TimeoutError("timed out")
            if "/documents/" in req.full_url:
                return _R(json.dumps({"body": "User: a donor", "updatedAt": "2026-11-01T05:00:00Z"}))
            if "/agents/" in req.full_url:
                return _R(json.dumps({"urlKey": "jobs"}))
            return _R(json.dumps({"title": "t", "assigneeAgentId": "a1"}))
        with tempfile.TemporaryDirectory() as d:
            env = {"PAPERCLIP_API_URL": "http://b", "PAPERCLIP_API_KEY": "k", "JUDGE_CARD_CACHE": d}
            with mock.patch.dict(os.environ, env), mock.patch.object(vc.urllib.request, "urlopen", _open):
                good = vc.fetch("PHIA-1")
                self.assertEqual((good["card"], good["owner"]), ("User: a donor", "jobs"))
                fail[0], calls[:] = True, []
                saved = vc.fetch("PHIA-1")
                self.assertEqual(len(calls), 2 * vc.TRIES)  # issue and card, each tried TRIES times
                self.assertEqual((saved["card"], saved["owner"]), ("User: a donor", "jobs"))
                self.assertIn("TimeoutError", saved["read_failed"])
                self.assertNotIn("error", saved)
                text = vj.apply(_scored(), "", "1", "h", "t", "g/x", False, saved)["scores_text"]
                self.assertIn(f"| Research (card, saved {saved['cached_at']}) |", text)
                self.assertIn("card unread: TimeoutError", vj.apply(_scored(), "", "1", "h", "t", "g/x", False,
                                                                    vc.fetch("PHIA-2"))["scores_text"])
        self.assertEqual(vc.TRIES, 2)

    def test_review_again_label_and_task_from_branch(self):
        import re as _re
        import subprocess
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        fn = _re.search(r"^pr_task\(\) \{.*?^\}", sh, _re.S | _re.M).group(0)
        jq = _re.search(r"jq -e --arg n \"\$pr\" --arg l \"\$AGAIN_LABEL\" '([^']+)'", sh).group(1)
        prs = json.dumps([{"number": 7, "title": "Quadrant words", "headRefName": "phia1217-quadrant-words",
                           "labels": [{"name": "review:again"}]},
                          {"number": 8, "title": "x (PHIA-12)", "headRefName": "g/y", "labels": []}])
        script = fn + f"\npr_task 7 '{prs}'; pr_task 8 '{prs}'\n" + "".join(
            f"printf '%s' '{prs}' | jq -e --arg n {n} --arg l review:again '{jq}' >/dev/null && echo again{n}\n"
            for n in (7, 8))
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["PHIA-1217", "PHIA-12", "again7"])
        self.assertIn("Paperwork findings from that round no longer apply", sh)
        self.assertIn('labels/${AGAIN_LABEL}', sh)

    def test_shell_reads_the_card_and_cuts_paperwork(self):
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        self.assertIn('--card "$CARD_FILE"', sh)
        self.assertIn("VALUE CARD", sh)
        self.assertNotIn("should carry '## Why'", sh)

    def test_a_correctness_block_stays_a_block(self):
        bug = [{"file": "a.py", "line": 1, "severity": "high", "what_breaks": "x", "plain": "y"}]
        out = vj.apply(_scored("block", 3, 3, 3, 3, findings=bug), "", "1", "h", "t", "r", False)
        self.assertEqual(out["verdict"], "block")

    def test_exempt_is_never_sent_back_on_value(self):
        out = vj.apply(_scored(v=0, research=False), "", "1", "h", "t", "r", True)
        self.assertEqual(out["verdict"], "approve")

    def test_an_unscored_verdict_passes_through(self):
        r = jv.parse(_envelope({"verdict": "approve", "findings": []}))
        self.assertEqual(vj.apply(r, "", "1", "h", "t", "r", False), r)

    def test_plan_line_and_match(self):
        body = "## Why\nx\n\nPlan: Value 2, Usability 3, Feasibility 2, Viability 1 (PHIA-9)\n"
        out = vj.apply(_scored(), body, "1", "h", "Fix (PHIA-77)", "story/phia77-x", False)
        self.assertEqual(out["ledger"]["plan"], {"value": 2, "usability": 3, "feasibility": 2, "viability": 1})
        self.assertTrue(out["ledger"]["plan_matched"])
        self.assertEqual(out["ledger"]["task"], "PHIA-77")
        off = vj.apply(_scored(v=3, u=3), "Plan: Value 0, Usability 1, Feasibility 2, Viability 2", "1", "h", "t", "r", False)
        self.assertFalse(off["ledger"]["plan_matched"])

    def test_summary_counts_shipped_points_and_planless_send_backs(self):
        day = 1793000000.0
        rows = [
            {"ts": day, "pr": "1", "seat": "jobs", "verdict": "block", "points": 0, "value_reasons": ["x"], "plan": None},
            {"ts": day + 60, "pr": "1", "seat": "jobs", "verdict": "approve", "points": 4.0, "value_reasons": [],
             "plan": {"value": 2}, "plan_matched": True},
            {"ts": day, "pr": "2", "seat": "g", "verdict": "approve", "points": 6.0, "value_reasons": [], "plan": None},
        ]
        s = vj.summary(rows, merged={"1"}, now=day + 100)
        self.assertEqual(s["points_per_seat_per_day"], [{"seat": "v-jobs", "day": "2026-10-26", "points": 4.0}])
        self.assertEqual(s["sent_back_without_plan"], 1)
        self.assertEqual(s["plan_match_share"], 1.0)

    def test_shell_schema_carries_the_same_scores_fragment(self):
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        line = next(l for l in sh.splitlines() if l.startswith("VERDICT_SCHEMA="))
        schema = json.loads(line.split("=", 1)[1].strip("'"))
        for k, v in vj.SCORES_SCHEMA.items():
            self.assertEqual(schema["properties"][k], v)
        self.assertIn("scores", schema["required"])
        self.assertIn("research", schema["required"])

    def test_a_red_main_fix_scores_on_the_fixes_it_releases(self):
        # Operations 11-02 (PHIA-2214): a gate-only fix scored Value 1 twice while main was red.
        obj = _scored(v=2, u=2, f=2, b=2)
        obj["beneficiary"].update(metric="990/searches", guess=0, cut=False, tool=True)
        out = vj.apply(obj, "", "15155", "h", "t", "operations/x", False)
        self.assertEqual(out["ledger"]["scores"]["value"], 2)
        self.assertEqual(out["verdict"], "approve")
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text() + (Path(__file__).resolve().parents[1] / "members/judge-judy/value-rubric.md").read_text()
        self.assertIn("red-main fix (Operations, PHIA-2214; Paul 11-02)", sh)
        self.assertIn("never value 3. Examples: app 15155", sh)
        self.assertIn("the deploy it unblocks", sh)


# A dark board costs a judge-judy pass one card budget, never more (PHIA-2276, 2026-11-02).
# fleet-kit#1840 retried the card read (two tries of 150 s, then the agent name read the same way), so
# one pass could wait 600 s on the board, and the app reviewer posted nothing for an hour after it merged.
# These tests run judge-judy.sh's own card-read line against a board that takes the connection and never
# answers, and check that the pass goes on within the budget, with the saved card or with none.
_JJ_SRC = (Path(__file__).resolve().parents[1] / "members" / "judge-judy" / "judge-judy.sh").read_text()
CARD_LINE = re.search(r"^\s*(timeout .*value_card\.py\" \"\$\{PR_TASK:-\}\".*)$", _JJ_SRC, re.M).group(1)


class DarkBoardTest(unittest.TestCase):
    def setUp(self):
        # listen() with no accept(): the kernel completes the handshake, then nothing is ever sent
        self.board = socket.socket()
        self.board.bind(("127.0.0.1", 0))
        self.board.listen(16)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def card_step(self, cache, **env):
        card_file = self.dir / "body.card"
        full = dict(os.environ, KIT_DIR=str(Path(__file__).resolve().parents[1]), PR_TASK="PHIA-7", CARD_FILE=str(card_file),
                    LOG=str(self.dir / "judge-judy.log"), JUDGE_CARD_CACHE=str(cache),
                    PAPERCLIP_API_URL=f"http://127.0.0.1:{self.board.getsockname()[1]}",
                    PAPERCLIP_API_KEY="k", JUDGE_CARD_TIMEOUT="150", JUDGE_CARD_TRIES="2",
                    JUDGE_CARD_BUDGET="3", **env)
        start = time.monotonic()
        subprocess.run(["bash", "-c", CARD_LINE], env=full, check=True, timeout=60)
        return time.monotonic() - start, json.loads(card_file.read_text())

    def test_the_card_step_ends_within_its_budget_and_uses_the_saved_read(self):
        cache = self.dir / "cache"
        cache.mkdir()
        (cache / "PHIA-7.json").write_text(json.dumps(
            {"task": "PHIA-7", "card": "User: a donor", "owner": "jobs", "cached_at": "2026-11-02T08:00:00Z"}))
        took, card = self.card_step(cache)
        self.assertLess(took, 6)  # budget 3 s; two 150 s tries before PHIA-2276
        self.assertEqual(card["card"], "User: a donor")
        self.assertIn("read_failed", card)

    def test_no_saved_read_means_no_card_and_the_pass_goes_on(self):
        took, card = self.card_step(self.dir / "cache")
        self.assertLess(took, 6)
        self.assertFalse(card.get("card"))
        self.assertIn("error", card)

    def test_an_unwritable_cache_dir_is_not_fatal(self):
        blocker = self.dir / "file"
        blocker.write_text("")
        took, card = self.card_step(blocker / "cache")  # a folder under a file can never be made
        self.assertLess(took, 6)
        self.assertIn("error", card)

    def card_wait(self, card, head="h1", waits="2"):
        # PHIA-2409: judge-judy.sh's own card_read_wait, run on a card file; True = no verdict this pass
        (self.dir / "c.json").write_text(json.dumps(card))
        fn = _JJ_SRC[_JJ_SRC.index("card_read_wait() {"):_JJ_SRC.index("\npost_status() {")]
        script = f'set -u\nAPPROVED_DIR={self.dir}/approved\n{fn}\ncard_read_wait 7 {head} {self.dir}/c.json\n'
        return subprocess.run(["bash", "-c", script], env=dict(os.environ, JUDGE_CARD_WAITS=waits), timeout=30, check=False).returncode == 0

    def test_a_timed_out_read_with_no_saved_card_waits_instead_of_scoring_blind(self):
        timed_out = {"task": "PHIA-7", "error": "TimeoutError", "task_text": "t"}
        self.assertTrue(self.card_wait(timed_out))
        self.assertTrue(self.card_wait(timed_out))
        self.assertFalse(self.card_wait(timed_out))  # 3rd pass on one head, cap 2: the review goes on
        self.assertTrue(self.card_wait(timed_out, head="h2"))  # a new head waits afresh
        self.assertTrue(self.card_wait({"task": "PHIA-7", "error": "card read hard stop"}, head="h3"))
        for card in ({}, {"task": "PHIA-7", "card": "x"}, {"task": "PHIA-7", "card": "", "task_text": "t"},
                     {"task": "PHIA-7", "card": "x", "error": "TimeoutError", "read_failed": "TimeoutError"},
                     {"task": "PHIA-7", "error": "HTTP 403"},
                     {"task": "PHIA-7", "error": "PAPERCLIP_API_URL or PAPERCLIP_API_KEY not in the judge's environment"}):
            self.assertFalse(self.card_wait(card, head="h4"), card)

    def test_the_card_step_waits_before_the_prompt_is_built(self):
        self.assertLess(_JJ_SRC.index('if card_read_wait "$PR" "$HEAD_SHA" "$CARD_FILE"'), _JJ_SRC.index("  CARD_TEXT=$(jq"))
        # the card read killed or crashed (no value_card.py under KIT_DIR) is a read that failed, not "no card"
        card_file = self.dir / "body.card"
        subprocess.run(["bash", "-c", CARD_LINE], check=True, timeout=30, env=dict(
            os.environ, KIT_DIR=str(self.dir), PR_TASK="PHIA-7", CARD_FILE=str(card_file), LOG=str(self.dir / "log")))
        self.assertEqual(json.loads(card_file.read_text()), {"task": "PHIA-7", "error": "card read hard stop"})
        subprocess.run(["bash", "-c", CARD_LINE], check=True, timeout=30, env=dict(
            os.environ, KIT_DIR=str(self.dir), PR_TASK="", CARD_FILE=str(card_file), LOG=str(self.dir / "log")))
        self.assertEqual(json.loads(card_file.read_text()), {})

    def test_the_hard_stop_follows_the_budget_and_the_cache_sits_by_the_log(self):
        self.assertTrue(CARD_LINE.startswith('timeout "$(( ${JUDGE_CARD_BUDGET:-150} + 15 ))s" python3'))
        self.assertIn('JUDGE_CARD_CACHE="${JUDGE_CARD_CACHE:-$LOG_DIR/value-card-cache}"', _JJ_SRC)
        self.assertIn("JUDGE_CARD_TIMEOUT=30 JUDGE_CARD_BUDGET=30 JUDGE_CARD_TRIES=1", _JJ_SRC)


class PromptBuildsTest(unittest.TestCase):
    def test_the_value_prompt_is_one_string_under_set_u(self):
        # PHIA-2267: #1840 and #1842 put "quoted examples" inside BODY_RULES="...". bash closed the
        # string there and ran `41` as a command, BODY_RULES was never set, and under set -u every
        # review pass died before posting a status: the app reviewer was silent twice (PHIA-2276, PHIA-2307).
        start = _JJ_SRC.index('  BODY_RULES="')
        end = _JJ_SRC.index('}"\n', start) + 3
        script = ('set -euo pipefail\nJUDGE_KPI_METRICS=x\nRULES_TEXT="\n--- rules ---"\n'
                  + f'KIT_DIR={Path(__file__).resolve().parents[1]}\n'
                  + _JJ_SRC[start:end] + 'printf %s "$BODY_RULES"\n')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        self.assertIn("Value 2 or 3 needs a named user and a measured baseline", out.stdout)
        # Reif 11-02: the rubric is one file, value-rubric.md, read by this gate and by G; both sections reach the prompt
        for line in ("- value: will people want it?", "- viability: does it work for the business?", "Anchors (real philanthropy PRs", "A score of 2 is a claim, not a default"):
            self.assertIn(line, out.stdout)
        self.assertLess(out.stdout.index("- viability:"), out.stdout.index("First fill beneficiary"))
        self.assertLess(out.stdout.index("First fill beneficiary"), out.stdout.index("Anchors (real"))
        self.assertTrue(out.stdout.endswith("--- rules ---"), out.stdout[-200:])


if __name__ == "__main__":
    unittest.main()
