#!/usr/bin/env python3
"""items_arg -- one reader for the `--items` argument gru passes to its gates and packer.

2026-09-25: quality_gate.py and vision_link_gate.py took `--items` ONLY as inline JSON on argv.
gru feeds them full issue bodies plus every comment, which overflows the kernel's argv limit
("Argument list too long"), so each pass hand-rolled its own workaround (python heredocs that
import the gate) instead of running the documented command. `--items` now takes any of:

    --items '[{"number": 1, ...}]'   inline JSON (a value starting with `[` or `{`)
    --items -                        JSON on stdin
    --items /tmp/gru_items.json      a path to a JSON file
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_items(value: str, stdin=None):
    text = value.lstrip()
    if text.startswith(("[", "{")):
        return json.loads(value)
    if value == "-":
        return json.loads((stdin or sys.stdin).read())
    return json.loads(Path(value).read_text())


HELP = ("JSON list inline, '-' for stdin, or a path to a JSON file "
        "(use a file or stdin for full issue bodies: inline argv overflows)")
