"""Regression test for vision_link_gate.py, gh#525's build-eligibility gate.

Written by dumbledore, 2026-09-07, after the gate produced FIVE separate live bugs in its
first ~30 hours (gh#576 started-row double-count, the #579 dash-less-maintenance fix, gh#584
dash-separator gap, gh#593 gate-vs-dead-end-filter ordering, gh#595 heading-style invisibility)
with zero regression coverage despite this repo's own test_*.py convention (see
test_needs_human_op.py, test_alert_store.py, etc.) covering every other script born around the
same time. Each of those bugs cost a live gru pass discovering it the hard way. This file locks
in every format variant already found live so the next one is caught here, not in gru.log.

Run: python3 scripts/test_vision_link_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import vision_link_gate as vlg  # noqa: E402


class ClassifyValueTests(unittest.TestCase):
    def test_real_link_is_linked(self):
        status, raw = vlg._classify_value("okr.verified_claims -- the number, gh#519")
        self.assertEqual(status, vlg.STATUS_LINKED)

    def test_maintenance_dash_form(self):
        # The original, always-worked form: "none (maintenance) -- <reason>".
        status, _ = vlg._classify_value("none (maintenance) -- fleet-internal tooling.")
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_maintenance_no_dash_trailing_qualifier_gh584(self):
        # gh#584: no dash separator at all, plus an extra qualifier inside the parens.
        status, _ = vlg._classify_value("none (fleet guardrail/maintenance).")
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_maintenance_case_and_spacing_insensitive(self):
        status, _ = vlg._classify_value("  None ( Maintenance )  ")
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_real_link_mentioning_maintenance_is_not_swallowed(self):
        # A real link that happens to use the word "maintenance" must not be mis-read as the
        # maintenance token just because the substring appears somewhere in the value.
        status, _ = vlg._classify_value(
            "okr.conversion -- reduces support/maintenance load on the on-call rotation -- gh#601"
        )
        self.assertEqual(status, vlg.STATUS_LINKED)

    def test_prose_without_a_registered_id_is_missing(self):
        # Reif 2026-09-16: OKRs were set and nothing bound the pick to them, because any prose
        # counted. Live example that passed: "KR2 -- sixteen members opened a thread".
        status, _ = vlg._classify_value("KR2 -- sixteen members opened a thread with us in 24h")
        self.assertEqual(status, vlg.STATUS_MISSING)
        status, _ = vlg._classify_value("Stripe MRR -- the number")
        self.assertEqual(status, vlg.STATUS_MISSING)
        self.assertEqual(set(vlg.kr_ids()), {"okr.verified_claims", "okr.traffic", "okr.clicks", "okr.conversion"})


class ClassifyCandidateTests(unittest.TestCase):
    def test_missing_when_no_vision_link_line(self):
        status, raw = vlg.classify_candidate("Just a plain issue body, no field at all.", [])
        self.assertEqual(status, vlg.STATUS_MISSING)
        self.assertIsNone(raw)

    def test_heading_style_is_missing_not_linked_gh595(self):
        # gh#595: marie.md's old template produced a `## Vision-link` heading with the value
        # on the next line. run_report._vision_claim requires the colon on the same line as
        # the label, so this must resolve to MISSING (never LINKED) -- a false LINKED would
        # silently starve every real maintenance candidate via the gate's crowd-out rule,
        # which is exactly what gh#593 named as the failure mode for a wrongly-LINKED value.
        body = "## Vision-link\nnone (maintenance) -- fleet-internal tooling.\n"
        status, raw = vlg.classify_candidate(body, [])
        self.assertEqual(status, vlg.STATUS_MISSING)

    def test_inline_form_is_maintenance(self):
        body = "## Out of scope\nNone.\n\nVision-link: none (maintenance) -- tooling only.\n"
        status, raw = vlg.classify_candidate(body, [])
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_newest_comment_wins_over_body(self):
        body = "Vision-link: none (maintenance) -- old value."
        comments = [
            {"body": "irrelevant earlier comment"},
            {"body": "Vision-link: okr.clicks -- rescored PRD."},
        ]
        status, raw = vlg.classify_candidate(body, comments)
        self.assertEqual(status, vlg.STATUS_LINKED)
        self.assertIn("okr.clicks", raw)

    def test_lightweight_non_prd_comment_is_linked_gh4597(self):
        # gh#4597: marie's PRD template (Part C4) only stamps this line on capped,
        # `fleet:priority-high`-only PRD comments -- most of the backlog never gets a
        # `fleet:prd` comment at all. The fix is a lightweight, PRD-less comment carrying just
        # the one line; this script has no notion of `fleet:prd`/labels at all (candidates here
        # carry no `labels` field), so a bare one-line comment must classify identically to a
        # full PRD comment saying the same thing.
        body = "A plain issue body with no Vision-link line of its own."
        comments = [{"body": "Vision-link: okr.verified_claims -- the number, gh#4597 lightweight backfill."}]
        status, raw = vlg.classify_candidate(body, comments)
        self.assertEqual(status, vlg.STATUS_LINKED)
        self.assertIn("okr.verified_claims", raw)

    def test_lightweight_non_prd_maintenance_comment_gh4597(self):
        body = "A plain issue body with no Vision-link line of its own."
        comments = [{"body": "Vision-link: none (maintenance) -- lightweight backfill, gh#4597."}]
        status, raw = vlg.classify_candidate(body, comments)
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)


class GateCandidatesOrderingTests(unittest.TestCase):
    def test_maintenance_only_pool_is_all_eligible(self):
        # gh#593: with no real-link candidate anywhere in the pool, every maintenance-only
        # candidate must be eligible -- a doomed-but-mislabeled LINKED candidate must never
        # be allowed to crowd out the whole pool (that half of gh#593 is claim_history's job
        # to filter before this gate runs; this gate's own contract is just: no linked
        # candidates present -> nothing gets dropped for "a linked-KR candidate is open").
        candidates = [
            {"number": 1, "body": "Vision-link: none (maintenance) -- a.", "comments": []},
            {"number": 2, "body": "Vision-link: none (maintenance) -- b.", "comments": []},
        ]
        result = vlg.gate_candidates(candidates)
        self.assertEqual(result["eligible"], [1, 2])
        self.assertEqual(result["dropped"], [])

    def test_real_link_never_crowds_out_maintenance_fk1191(self):
        # fk#1191: one linked ticket used to hide every maintenance item (117 of 184 on the
        # 2026-09-21 board). Ranking orders them; the gate must not blind them.
        candidates = [
            {"number": 1, "body": "Vision-link: none (maintenance) -- a.", "comments": []},
            {"number": 2, "body": "Vision-link: okr.traffic -- b.", "comments": []},
            {"number": 3, "body": "no line here", "comments": []},
        ]
        result = vlg.gate_candidates(candidates)
        self.assertEqual(result["eligible"], [1, 2])
        self.assertEqual([d["number"] for d in result["dropped"]], [3])


if __name__ == "__main__":
    unittest.main()
