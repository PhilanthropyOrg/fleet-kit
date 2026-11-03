#!/usr/bin/env python3
"""Print the full text, at the PR head, of each file a diff changes.

judge-judy reviews with no tools (test_judge_judy_no_tools.py): the model sees only what is in
the prompt. With only diff hunks, about 3 in 10 reviews on 2026-11-03 said "I could not open the
files" (the route around a hunk, the test file, the workflow) and scored blind (PHIA-1451).
This puts the changed files themselves in the prompt, read with `git show <sha>:<path>`, so
the review can read the code around each hunk without a shell.

Usage: head_files.py <sha> [budget_bytes] [per_file_bytes] < diff
Deleted, binary and over-size files are named, not printed. Output is capped at budget_bytes.
"""

from __future__ import annotations

import subprocess
import sys


def changed_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:].rstrip("\t")
            if p not in paths:
                paths.append(p)
    return paths


def show(sha: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "show", f"{sha}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def render(diff: str, sha: str, budget: int = 150000, per_file: int = 60000) -> str:
    out: list[str] = []
    used = 0
    skipped: list[str] = []
    for path in changed_paths(diff):
        raw = show(sha, path)
        if raw is None:
            skipped.append(f"{path} (could not read at this head)")
            continue
        if b"\0" in raw[:8000]:
            skipped.append(f"{path} (binary)")
            continue
        if len(raw) > per_file:
            skipped.append(f"{path} (too big: {len(raw)} bytes; its hunks are in the diff)")
            continue
        text = raw.decode("utf-8", "replace")
        block = f"===== FILE {path} (full text at this head) =====\n{text}\n"
        if used + len(block.encode()) > budget:
            skipped.append(f"{path} (over the file budget; its hunks are in the diff)")
            continue
        out.append(block)
        used += len(block.encode())
    if skipped:
        out.append("Not shown here:\n" + "\n".join(f"- {s}" for s in skipped) + "\n")
    return "".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    budget = int(argv[2]) if len(argv) > 2 else 150000
    per_file = int(argv[3]) if len(argv) > 3 else 60000
    sys.stdout.write(render(sys.stdin.read(), argv[1], budget, per_file))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
