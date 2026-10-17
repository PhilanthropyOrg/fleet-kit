#!/usr/bin/env python3
"""intent_repeats.py -- did Reif have to say it twice? (Reif, 2026-10-17 00:05 Central)

"I don't want to keep having to explain this daily." INTENT.md was last written 10-15 00:22,
so nothing he said on 10-16 (hospitals out of Jobs, one Overview, test links to gmail,
auditors) reached a member, and he said several of them again. A repeat is the fleet's miss.

THE KEYS. INTENT.md opens with a `## Keys` section, one line per direction:
    - 2026-10-16 hospitals: hospital | healthcare system
The date is the day he first said it; phrases are lowercase substrings. A desk inbox item
(DESK_DIR/YYYYMMDD-HHMMSS.md, Central time) dated AFTER a key's day whose text holds one of
its phrases is a repeat. Keys only grow: librarian adds one for every new direction.

  repeats [--hours 24]   one line per repeat in the window: item, key
  pregate                `green` when no desk item is newer than INTENT.md and it is not the
                         00:xx daily pass; else `intent: N new desk item(s)` or `daily`
fleet_metrics.py's `reif_repeats_per_day` is count_repeats() over its window.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
KEY_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) ([\w.+-]+): (.+)$")
ITEM_NAME = re.compile(r"^(\d{8}-\d{6})\.md$")


def intent_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "INTENT.md"


def desk_dir() -> Path:
    return Path(os.environ.get("FLEET_DESK_INBOX", "/fleet-kit/shares/g/inbox-copy"))


def parse_keys(text: str) -> list[tuple[str, str, list[str]]]:
    """(day, name, phrases) for each line of the `## Keys` section."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Keys"
            continue
        m = KEY_LINE.match(line.strip()) if inside else None
        if m:
            phrases = [p.strip().lower() for p in m.group(3).split("|") if p.strip()]
            out.append((m.group(1), m.group(2), phrases))
    return out


def desk_items(d: Path | None = None) -> list[tuple[float, str, str]]:
    """(epoch, file name, text) for every desk inbox file, oldest first."""
    d = d or desk_dir()
    out = []
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        m = ITEM_NAME.match(p.name)
        if not m:
            continue
        when = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=CENTRAL)
        out.append((when.timestamp(), p.name, p.read_text(errors="ignore")))
    return out


def repeats(items, keys) -> list[tuple[str, str]]:
    """(file name, key name) for every item that says a key again after the key's day."""
    out = []
    for ts, name, text in items:
        day = dt.datetime.fromtimestamp(ts, CENTRAL).strftime("%Y-%m-%d")
        low = text.lower()
        for kday, kname, phrases in keys:
            if kday < day and any(p in low for p in phrases):
                out.append((name, kname))
                break
    return out


def count_repeats(at: float, hours: float, intent: Path | None = None,
                  d: Path | None = None) -> int | None:
    """Repeats among desk items in (at - hours, at]; None when INTENT.md has no keys or the
    desk dir is missing (unmeasured, never 0)."""
    try:
        keys = parse_keys((intent or intent_path()).read_text(errors="ignore"))
    except OSError:
        return None
    d = d or desk_dir()
    if not keys or not d.is_dir():
        return None
    win = [i for i in desk_items(d) if at - hours * 3600 < i[0] <= at]
    return len(repeats(win, keys))


def pregate(now: float, intent: Path | None = None, d: Path | None = None) -> str:
    intent = intent or intent_path()
    if dt.datetime.fromtimestamp(now, CENTRAL).hour == 0:
        return "daily"
    since = intent.stat().st_mtime if intent.exists() else 0.0
    new = [i for i in desk_items(d) if i[0] > since]
    return f"intent: {len(new)} new desk item(s): {', '.join(n for _, n, _ in new)}" if new else \
        "green: no desk item newer than INTENT.md"


def main(argv: list[str]) -> int:
    now = time.time()
    if argv[1:2] == ["pregate"]:
        print(pregate(now))
        return 0
    if argv[1:2] == ["repeats"]:
        hours = float(argv[3]) if argv[2:3] == ["--hours"] else 24.0
        keys = parse_keys(intent_path().read_text(errors="ignore")) if intent_path().exists() else []
        items = [i for i in desk_items() if i[0] > now - hours * 3600]
        for name, key in repeats(items, keys):
            print(f"{name} says `{key}` again")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
