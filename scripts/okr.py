#!/usr/bin/env python3
"""okr.py -- the ONE place the fleet reads its goals from.

Two readers used to open scripts/okr.json on their own (north.py for NORTH.md,
vision_link_gate.py for the ids a `Vision-link:` line may name). The kit's own file holds one
product's goal, so a second startup on the same kit would be ranked against someone else's.
A product now carries its goals in its own repo, and every reader asks here.

Where the goals come from, first hit wins:

  1. FLEET_OKR_FILE   -- the operator's explicit override, exactly as before.
  2. $FLEET_REPO/fleet/okr.json -- the product repo's own goals (fleet_init.py writes it).
  3. scripts/okr.json -- the kit's file, exactly as before. An instance whose product repo has
     no fleet/okr.json (the live one today) reads the same bytes it always did.

A product file that is there but unusable (bad JSON, no objective id) is skipped with one
line on stderr naming the file and the reason, and the kit's file is used: a typo in a product
repo must never take down every member that reads the goals.

  okr.py ids     -> one registered id per line (what a Vision-link may name)
  okr.py path    -> the file the goals were read from
  okr.py show    -> the goals as JSON
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT_FILE = HERE / "okr.json"
PRODUCT_FILE = Path("fleet") / "okr.json"


def problem(data) -> str | None:
    """Why this is not a usable goals file, or None when it is. The shape is scripts/okr.json's:
    {"objective": {"id", "label"}, "key_results": [{"id", "label"}, ...]}."""
    if not isinstance(data, dict):
        return "top level is not an object"
    obj = data.get("objective")
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), str) or not obj["id"].strip():
        return "no objective.id"
    if not isinstance(obj.get("label"), str):
        return "no objective.label"
    krs = data.get("key_results", [])
    if not isinstance(krs, list):
        return "key_results is not a list"
    for k in krs:
        if not isinstance(k, dict) or not isinstance(k.get("id"), str) or not isinstance(k.get("label"), str):
            return "a key result has no id or label"
    return None


def _product_file() -> Path | None:
    repo = os.environ.get("FLEET_REPO") or ""
    return Path(repo).expanduser() / PRODUCT_FILE if repo else None


def source(path: Path | None = None) -> Path:
    """The file load() reads. A broken product file is reported on stderr and passed over."""
    if path:
        return Path(path)
    if os.environ.get("FLEET_OKR_FILE"):
        return Path(os.environ["FLEET_OKR_FILE"])
    p = _product_file()
    if p is not None and p.is_file():
        try:
            why = problem(json.loads(p.read_text()))
        except (OSError, ValueError) as exc:
            why = f"unreadable: {str(exc)[:120]}"
        if why is None:
            return p
        print(f"okr: {p} is not usable ({why}) -- using the kit's goals at {KIT_FILE} instead. "
              f"Fix the product repo's file.", file=sys.stderr)
    return KIT_FILE


def load(path: Path | None = None) -> dict:
    """The goals, or {} when the chosen file cannot be read (what both readers did before)."""
    try:
        data = json.loads(source(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ids(path: Path | None = None) -> list[str]:
    """The objective's id plus every key result's: what a Vision-link line may name. Empty when
    the file is unreadable -- and then nothing can be linked, which is loud on purpose."""
    try:
        d = load(path)
        return [d["objective"]["id"]] + [k["id"] for k in d.get("key_results", [])]
    except Exception:  # noqa: BLE001
        return []


def main(argv=None) -> int:
    cmd = (argv if argv is not None else sys.argv[1:]) or ["ids"]
    if cmd[0] == "ids":
        print("\n".join(ids()))
    elif cmd[0] == "path":
        print(source())
    elif cmd[0] == "show":
        print(json.dumps(load(), indent=2))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
