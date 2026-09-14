"""Regression test for closes_gate.py's decomposed-children parser (fk#882): marie's
`decomposed into #a, #b, ...` comment almost always carries a per-child `(seq:N -- ...)`
parenthetical, or a range/slash shorthand, none of which the old `,`/`and`-only alternation
tolerated -- it silently truncated after the first entry, so `epic_closable()` reported
`closable: true` on a list it had only partly read. Covers AC1-6 of the fk#882 PRD comment
(2026-09-11). No network.

Run: python3 scripts/test_closes_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import closes_gate as cg  # noqa: E402


def comment(created_at, body):
    return {"createdAt": created_at, "body": body}


class DecomposedChildrenTests(unittest.TestCase):
    def test_ac1_parenthetical_descriptions_do_not_truncate_the_list(self):
        body = (
            "decomposed into #560 (home=the number), #561 (percent-of-week topbar), "
            "#562 (collapse started+completion rows), #563 (Stats skeletons+perf+bold verdict), "
            "#564 (rename Self-Evolution tab), #565 (members list visual states) (Part C2b) "
            "— tracking-only from here"
        )
        iss = {"comments": [comment("2026-09-06T03:42:49Z", body)]}
        self.assertEqual(cg.decomposed_children(iss), [560, 561, 562, 563, 564, 565])

    def test_ac2_parenthetical_marker_is_not_read_as_a_child(self):
        body = "decomposed into #560 (home), #561 (topbar) (Part C2b) — tracking-only from here"
        iss = {"comments": [comment("x", body)]}
        self.assertNotIn(2, cg.decomposed_children(iss))
        self.assertEqual(cg.decomposed_children(iss), [560, 561])

    def test_ac3_bare_comma_list_still_works(self):
        body = "decomposed into #794, #795, #796 (Part C2b) — tracking-only from here"
        iss = {"comments": [comment("x", body)]}
        self.assertEqual(cg.decomposed_children(iss), [794, 795, 796])

    def test_ac4_range_shorthand_returns_only_the_named_endpoints(self):
        iss = {"comments": [comment("x", "decomposed into #4642-#4645")]}
        self.assertEqual(cg.decomposed_children(iss), [4642, 4645])

    def test_ac4_slash_shorthand_returns_every_number(self):
        iss = {"comments": [comment("x", "decomposed into #794/#795/#796")]}
        self.assertEqual(cg.decomposed_children(iss), [794, 795, 796])

    def test_ac5_prose_quoting_the_phrase_with_no_real_numbers_yields_empty_and_does_not_overwrite(self):
        iss = {
            "comments": [
                comment("2026-09-01T00:00:00Z", "marie: decomposed into #10, #11 (Part C2b)"),
                comment("2026-09-11T06:07:33Z", "this issue was decomposed into #a, #b, … per the filer"),
            ]
        }
        self.assertEqual(cg.decomposed_children(iss), [10, 11])

    def test_ac6_a_child_whose_state_could_not_be_fetched_blocks_the_epic(self):
        epic = {
            "title": "epic",
            "labels": [{"name": "fleet:epic"}],
            "body": "",
            "comments": [comment("a", "decomposed into #10, #11 (Part C2b)")],
        }
        # #11 is absent from `issues` -- a fetch failure, not a confirmed close
        issues = {634: epic, 10: {"state": "CLOSED"}}
        ec = cg.epic_closable(epic, issues)
        self.assertFalse(ec["closable"])
        self.assertIn("11", ec["reason"])

    def test_ac6_all_children_confirmed_closed_is_still_closable(self):
        epic = {
            "title": "epic",
            "labels": [{"name": "fleet:epic"}],
            "body": "",
            "comments": [comment("a", "decomposed into #10, #11 (Part C2b)")],
        }
        issues = {634: epic, 10: {"state": "CLOSED"}, 11: {"state": "CLOSED"}}
        ec = cg.epic_closable(epic, issues)
        self.assertTrue(ec["closable"])


# The real body of philanthropy#5492 (head 16f6673a66d2) -- the PR the bare-digit false
# positive was blocking (fk-item5620 / philanthropy#5620). Verbatim, not a paraphrase, per
# marie's PRD AC4/AC5 (2026-09-13T08:05:10Z comment).
PHILANTHROPY_PR_5492_BODY = """\
## What this does
Someone who paid $10 for network verification, started the photo-ID check, and put their phone \
down currently hears nothing from us again — the "finish the ID check" reminder only ever \
showed up if they happened to land on their own account page.

## What changed
This is fix 5 of the 7-item VP round-2 work order on gh#5084 — `Part of #5084`, not a close.

- `_masthead.html`: the banner itself, deliberately **outside** `.mutil` (the nav utility row \
already regressed once at phone widths from a similar addition — gh#5453/fix 1) — a full-width \
strip below the nav instead.

**Still open on gh#5084** (left for a follow-up, not silently dropped):
- **Fix 3** (post the entitled/verified/stuck-unverified counts to the issue thread): \
re-investigated this pass.
- **Fix 6** (capped email nudge via `lifecycle_messaging`, 72h/14d): the in-product banner in \
this PR is the VP-specified first step.

**See it:** (internal) — not reproducible from a fresh anonymous view of production, confirming \
placement and that it doesn't crowd the nav row fix 1 already had to fix once.
"""


class ClosingNumbersTests(unittest.TestCase):
    """fk-item5620 (philanthropy#5620): CLOSE_RE used to make the `#` optional, so ordinary
    prose like "fix 5 of the" or "**Fix 3**" parsed as a real closing reference. Covers AC1-4
    of marie's PRD comment (philanthropy#5620, 2026-09-13T08:05:10Z)."""

    def test_ac1_bare_number_prose_is_not_a_closing_reference(self):
        body = "This is fix 5 of the 7-item VP round-2 work order on gh#5084 — `Part of #5084`, not a close."
        self.assertNotIn(5, cg.closing_numbers(body))

    def test_ac2_bolded_and_bare_prose_forms_are_ignored_but_real_keyword_still_caught(self):
        body = "**Fix 3**, **Fix 6**, fix 1 and Fixes #4321 together."
        self.assertEqual(cg.closing_numbers(body), [4321])

    def test_ac3_every_real_closing_keyword_still_detected_with_hash(self):
        for kw in ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"):
            with self.subTest(kw=kw):
                self.assertEqual(cg.closing_numbers(f"{kw} #42"), [42])

    def test_ac3_every_real_closing_keyword_still_detected_with_gh_hash(self):
        for kw in ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"):
            with self.subTest(kw=kw):
                self.assertEqual(cg.closing_numbers(f"{kw} gh#42"), [42])

    def test_ac4_philanthropy_pr_5492_real_body_yields_no_closing_reference(self):
        self.assertEqual(cg.closing_numbers(PHILANTHROPY_PR_5492_BODY), [])


if __name__ == "__main__":
    unittest.main()
