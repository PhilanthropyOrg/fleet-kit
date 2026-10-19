"""philanthropy#13212 (Reif 2026-10-19, "its for every bloody page type"): the reviewer's body
rules require a design walk on every PR that changes what a person sees, opened after the rule.
Mutation: delete the design-walk block from judge-judy.sh -> these go RED."""
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()


def test_design_walk_rule_is_in_the_body_rules():
    assert "'Design walk:'" in SH
    assert "1440 wide" in SH and "390 wide" in SH
    assert "rendered mail, not the template source" in SH
    assert "Missing walk: set your verdict to block" in SH


def test_rule_spares_prs_opened_before_it():
    assert '"2026-10-19T18:00:00Z"' in SH
    # appended to BODY_RULES before the dependabot override, so a pin-only bump still waives it
    assert SH.index("Design walk:") < SH.index('select(.author.login == "app/dependabot"')
