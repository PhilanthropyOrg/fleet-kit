"""The fleet's one human clock: America/Chicago. Machines keep UTC -- ISO `...Z`, epochs,
fleet.db values and day/slot math never change -- only what a human reads is Central.

Log stamps are "YYYY-MM-DD HH:MM:SS <ZONE>". Lines written before the switch say UTC; lines
after say CDT or CST. Both stay in the same files, so every reader goes through parse_stamp.
"""
from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
# The abbreviation pins the offset exactly (CDT is always -5, CST always -6), so a stamp is
# never ambiguous -- not even in the repeated hour when DST ends.
_OFFSETS = {"UTC": 0, "GMT": 0, "Z": 0, "CDT": -5, "CST": -6}
ZONE_RE = r"(?:UTC|GMT|CDT|CST)"


def stamp(fmt: str = "%Y-%m-%d %H:%M:%S %Z", ts: float | None = None) -> str:
    """Now (or epoch `ts`) on the Central clock, zone name included."""
    when = _dt.datetime.now(CENTRAL) if ts is None else _dt.datetime.fromtimestamp(ts, CENTRAL)
    return when.strftime(fmt)


def parse_stamp(text: str, zone: str = "UTC", fmt: str = "%Y-%m-%d %H:%M:%S") -> _dt.datetime | None:
    """A log stamp's wall-clock text + zone abbreviation -> an aware datetime. None if unreadable."""
    off = _OFFSETS.get((zone or "UTC").upper())
    if off is None:
        return None
    try:
        naive = _dt.datetime.strptime(text, fmt)
    except ValueError:
        return None
    return naive.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=off)))
