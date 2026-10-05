#!/usr/bin/env python3
"""Fold whole-file deletions in a unified diff to one line each.

judge-judy cuts a diff at MAX_DIFF_BYTES. A cleanup PR that deletes big files used up the
whole budget on text that is going away, so the reviewer never saw the rest and blocked
(philanthropy gh#11182: 71,000 deleted lines, cut off inside a CSV). A deleted file's old
content tells the reviewer little; its NAME is what matters (does anything still point at it?).

Reads a diff on stdin, writes it to stdout. Each file section marked ``deleted file mode``
becomes its header plus one line: ``[whole file deleted: N lines]``. Everything else is
byte-for-byte unchanged.
"""

from __future__ import annotations

import sys


def fold(diff: str) -> str:
    out: list[str] = []
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        if any(line.startswith("deleted file mode") for line in section[:6]):
            head = [
                s
                for s in section
                if not s.startswith(("-", "@@", "\\")) or s.startswith("--- ")
            ]
            gone = sum(
                1 for s in section if s.startswith("-") and not s.startswith("--- ")
            )
            out.extend(head)
            out.append(f"[whole file deleted: {gone} lines]\n")
        else:
            out.extend(section)
        section.clear()

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
        section.append(line)
    flush()
    return "".join(out)


if __name__ == "__main__":
    sys.stdout.write(fold(sys.stdin.read()))
