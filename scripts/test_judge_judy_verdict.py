"""judge_judy_verdict.py: every finding carries a plain-English sentence (Reif, 2026-09-12:
"I want all reports to be written in plain english"), and the rendered text leads with it."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
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


def _scored(verdict="approve", v=2, u=2, f=2, b=2, research=True, findings=None):
    obj = {"verdict": verdict, "findings": findings or [],
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
        self.assertEqual(out["ledger"]["seat"], "jobs")

    def test_each_send_back_rule(self):
        card = {"task": "PHIA-9", "owner": "jobs", "card": "User: a donor\nBest in class: ?", "plan": ""}
        for kw, word in (({"v": 0}, "Value is 0"), ({"u": 0}, "Usability is 0"),
                         ({"research": False}, "value card on PHIA-9 is thin"),
                         ({"v": 1, "u": 1, "f": 1, "b": 1}, "Too little value")):
            out = vj.apply(_scored(**kw), "", "1", "abc", "t", "g/x", False, card)
            self.assertEqual(out["verdict"], "block", kw)
            self.assertIn(word, out["findings_text"], kw)

    def test_no_research_section_is_never_a_send_back(self):
        # Paul 10-31: 10 of 10 vertical PRs had no ## Research; with no card there is no hold.
        out = vj.apply(_scored(research=False), "## Why\nx", "1", "h", "t", "g/x", False)
        self.assertEqual(out["verdict"], "approve")
        out = vj.apply(_scored(research=False), "", "1", "h", "t", "g/x", False,
                       {"task": "PHIA-9", "owner": "g", "card": "", "plan": ""})
        self.assertEqual(out["verdict"], "approve")

    def test_card_owner_is_the_seat_and_its_self_score_is_logged_blind(self):
        card = {"task": "PHIA-1217", "owner": "atlas", "card": "User: x",
                "plan": "Self-score: Value 2, Usability 2, Feasibility 2, Viability 2"}
        out = vj.apply(_scored(), "", "1", "h", "Quadrant words", "phia1217-quadrant-words", False, card)
        self.assertEqual(out["ledger"]["seat"], "atlas")
        self.assertEqual(out["ledger"]["task"], "PHIA-1217")
        self.assertTrue(out["ledger"]["plan_matched"])
        self.assertTrue(out["ledger"]["card"])

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
            self.assertEqual(vc._get("/api/issues/PHIA-1"), {})
            del os.environ["PAPERCLIP_HOST"]
            vc._get("/api/issues/PHIA-1")
        self.assertEqual(seen[0], ("http://172.18.0.3:3100/api/issues/PHIA-1", "localhost:3100", "Bearer k"))
        self.assertIsNone(seen[1][1])

    def test_review_again_label_and_task_from_branch(self):
        import re as _re
        import subprocess
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()
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
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()
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
        self.assertEqual(s["points_per_seat_per_day"], [{"seat": "jobs", "day": "2026-10-26", "points": 4.0}])
        self.assertEqual(s["sent_back_without_plan"], 1)
        self.assertEqual(s["plan_match_share"], 1.0)

    def test_shell_schema_carries_the_same_scores_fragment(self):
        sh = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()
        line = next(l for l in sh.splitlines() if l.startswith("VERDICT_SCHEMA="))
        schema = json.loads(line.split("=", 1)[1].strip("'"))
        for k, v in vj.SCORES_SCHEMA.items():
            self.assertEqual(schema["properties"][k], v)
        self.assertIn("scores", schema["required"])
        self.assertIn("research", schema["required"])


if __name__ == "__main__":
    unittest.main()
