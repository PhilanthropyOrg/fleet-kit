#!/usr/bin/env python3
"""redact_secrets.py -- keep a leaked credential out of the run log and out of the next prompt.

A member's Outcome/Evidence text is written to runs.jsonl by run_report.py, shown on the
dashboard, and fed back into every later pass through HANDOFF.md and NORTH.md. Before this, a
token a member printed by mistake rode that loop forever: the hourly scrub only ever cleaned
session transcripts.

ONE pattern list: members/librarian/librarian.py's PATTERNS, the list the transcript scrub
already runs. A new credential class is added THERE, once, and this module gets it.
(intent_capture.py keeps its own copy on purpose -- it runs on a machine with no kit checkout
-- and selftest holds that copy to this list.)

Two things this adds on top of that list, both only for text people and prompts read:

  * the VALUES this process itself holds. Any env var whose name ends in _TOKEN/_KEY/_SECRET/
    _PASSWORD and whose value is at least 12 characters is replaced wherever that exact value
    appears -- no pattern needed, so a credential with no recognisable prefix is still caught.
  * a narrower `secret_env`. The scrub's NAME=value rule redacts anything after the `=`, which
    is right for a raw transcript and wrong for prose: "wired GH_TOKEN=$(cat ...) into cron"
    names a variable, it does not leak one. Here it only fires on a literal-looking value.

Redaction is not rotation (librarian.py's own header): a credential that reached a log is
burned. This only stops it spreading further.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "members" / "librarian"))
import librarian  # noqa: E402  -- the one pattern list

PATTERNS = librarian.PATTERNS

_SECRET_NAME_RE = re.compile(r"(?:_TOKEN|_KEY|_SECRET|_PASSWORD)$")
ENV_VALUE_MIN_LEN = 12
# A literal credential: one run of token characters with a digit in it. `$(cat ...)`, `$VAR`,
# `<tok>`, `'` and `REPLACE_ME` are how docs and prose NAME a variable, and stay readable.
_LITERAL_VALUE_RE = re.compile(r"[\"']?(?=[^\"']*[0-9])[A-Za-z0-9_\-./+=:@~,]{12,}[\"',;.)]*")


def is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME_RE.search(name or ""))


_OWN_ENV: list[tuple[str, str]] | None = None  # this process's env does not change: read it once


def _env_values(env=None) -> list[tuple[str, str]]:
    """(value, name) for every secret-named env var worth hunting, longest value first so a
    value that contains another is replaced whole. A value that is a path to a key FILE
    (GSC_SA_KEY, FLEET_PROD_PROBE_KEY) is not the secret and is left alone."""
    global _OWN_ENV
    if env is None and _OWN_ENV is not None:
        return _OWN_ENV
    out = [(v, k) for k, v in (os.environ if env is None else env).items()
           if is_secret_name(k) and len(v) >= ENV_VALUE_MIN_LEN and not v.startswith(("/", "~", "./"))]
    out.sort(key=lambda kv: -len(kv[0]))
    if env is None:
        _OWN_ENV = out
    return out


def _narrow_secret_env(sub_fn):
    return lambda m: sub_fn(m) if _LITERAL_VALUE_RE.fullmatch(m.group(2)) else m.group(0)


_SUBS = [(cls, pattern, _narrow_secret_env(sub_fn) if cls == "secret_env" else sub_fn)
         for cls, pattern, sub_fn in PATTERNS]


# One search that answers "could ANY class match?" -- a dashboard feed is thousands of short
# strings, nearly all of them clean, and asking each class in turn costs more than the scan.
_ANY_HINT_RE = re.compile("|".join(sorted({re.escape(h) for hs in librarian.HINTS.values() for h in hs})))
_UNHINTED = [cls for cls, _p, _s in PATTERNS if cls not in librarian.HINTS]


def redact_text(text, env=None):
    """`text` with every credential replaced by a [REDACTED:<class>] marker. Non-strings and
    empty strings come back untouched."""
    if not text or not isinstance(text, str):
        return text
    for value, name in _env_values(env):
        if value in text:
            text = text.replace(value, f"[REDACTED:env:{name}]")
    if not _UNHINTED and not _ANY_HINT_RE.search(text):
        return text
    for cls, pattern, sub_fn in _SUBS:
        if librarian.could_match(cls, text):
            text = pattern.sub(sub_fn, text)
    return text


def redact_record(obj, env=None):
    """A copy of a parsed JSON value (a runs.jsonl row) with every string in it redacted, at any
    depth. Works on parsed values, never on a serialized line: a `\\S+` match on raw JSON would
    run through the closing quote and break the row."""
    if isinstance(obj, str):
        return redact_text(obj, env)
    if isinstance(obj, dict):
        return {k: redact_record(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_record(v, env) for v in obj]
    return obj


def safe_record(obj):
    """redact_record that can never cost a caller its data: a bug here must not lose a run
    record or blank the dashboard, so any failure hands the row back as it came."""
    try:
        return redact_record(obj)
    except Exception:  # noqa: BLE001
        return obj


def safe_text(text):
    try:
        return redact_text(text)
    except Exception:  # noqa: BLE001
        return text


if __name__ == "__main__":
    sys.stdout.write(redact_text(sys.stdin.read()))
