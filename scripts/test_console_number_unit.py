"""The console formats the number by the payload's unit, never a hardcoded "$ … /mo".

fleet-kit: the objective moved from MRR to verified claims (3,000 by 2026-12-31) and the
console kept printing "$98/mo" for 98 claims, because renderNumber() hardcoded money().
"""
import re
from pathlib import Path

HTML = (Path(__file__).parent / "fleet_home.html").read_text()


def _render_number_block() -> str:
    m = re.search(r"function renderNumber\(\) \{(.*?)\n\}", HTML, re.S)
    assert m, "renderNumber() missing"
    return m.group(1)


def test_number_is_not_hardcoded_money():
    block = _render_number_block()
    assert "/mo</span>" not in block
    assert "${money(n.value)}" not in block
    assert "${fmt(n.value)}" in block


def test_number_shows_kr1_row():
    assert "p.kr1" in _render_number_block()
