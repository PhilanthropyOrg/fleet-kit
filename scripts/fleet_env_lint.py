#!/usr/bin/env python3
"""fleet_env_lint -- a fleet.env key assigned twice is a question nobody can answer by reading.

fk#1149: `FLEET_GRU_ALLOWANCE_FRACTION` was set to 0.9 at line 154 and 0.6 at line 194 of the
philanthropy instance's fleet.env, with FOUR other keys in the same state. Bash takes the last
assignment, so the live value was 0.6 -- but "what share of headroom may the fleet spend?" took
reading the whole file, and the handoff that noticed it first reported the wrong number. The
file grows by appended blocks from ssh sessions ("dials set 2026-09-02 via ssh"), so this will
recur; the check runs on every auto_deploy tick and names the key and both lines.

Pure `duplicate_keys(text)` for the test; `main()` is the exec seam (exit 1 on any duplicate).
"""
from __future__ import annotations
import re
import sys

_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def duplicate_keys(text: str) -> dict[str, list[int]]:
    """{key: [1-based line numbers]} for every key assigned more than once. Comments and
    blank lines are not assignments; a commented-out `# KEY=` is not one either."""
    seen: dict[str, list[int]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        m = _ASSIGN.match(line)
        if m:
            seen.setdefault(m.group(1), []).append(n)
    return {k: v for k, v in seen.items() if len(v) > 1}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fleet_env_lint.py <fleet.env>", file=sys.stderr)
        return 2
    try:
        text = open(argv[1]).read()
    except OSError as exc:
        print(f"fleet_env_lint: cannot read {argv[1]}: {exc}", file=sys.stderr)
        return 2
    dups = duplicate_keys(text)
    for k, lines in sorted(dups.items()):
        print(f"WARN {argv[1]}: {k} assigned {len(lines)}x (lines {', '.join(map(str, lines))}); "
              f"line {lines[-1]} wins, the rest mislead a reader")
    return 1 if dups else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
