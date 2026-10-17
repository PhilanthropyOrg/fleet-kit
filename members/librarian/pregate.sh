#!/bin/bash
# librarian's llm.pregate (Reif 2026-10-17: every direction reaches INTENT.md the same hour).
# `green` (no model, $0) unless a desk inbox item is newer than INTENT.md, or it is the 00:xx
# full daily read. See scripts/intent_repeats.py.
exec python3 "$(dirname "$0")/../../scripts/intent_repeats.py" pregate
