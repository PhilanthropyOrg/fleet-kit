#!/usr/bin/env python3
"""gru_allowance.py -- gru's hourly allowance, in PERCENT-OF-WEEK units.

WHY THIS IS A SCRIPT AND NOT PROSE IN gru.md: the charter itself says "do not do this
arithmetic in your head -- you are provably bad at it," and then asked gru to do exactly
that. It got it wrong in a way no one could see from the outside, for two compounding
reasons (Reif, 2026-09-02, auditing the Settings dials):

  1. WRONG BASE. The charter said to multiply `per_diem_hourly_pct`. That field is the hour's
     burn SO FAR -- consumption, not headroom. gru was slicing the wrong number: as the hour
     got MORE expensive, gru's computed allowance went UP.

  2. WRONG COMPOSITION. FLEET_SHARE_FRACTION and FLEET_GRU_ALLOWANCE_FRACTION were combined
     with min(), but they answer two nested questions and must MULTIPLY:

         "what share of the whole account may this instance use?"   FLEET_SHARE_FRACTION
         "what share of OUR slice may gru have?"                    FLEET_GRU_ALLOWANCE_FRACTION

     Under min() the smaller one simply won and the other became dead config. Live on
     fleet-kit-server-fleet: per_diem_hourly_pct=0.349, ceiling(0.20)=0.0142, so
     min(0.349*F, 0.0142) == 0.0142 for ANY F above ~0.04. Setting the dial to 0.25, 0.75 or
     0.99 produced an identical number, and that number was the instance's ENTIRE slice --
     gru took 100% of it, leaving nothing reserved for the other eight members.

The correct composition is one multiply on the already-coordinated ceiling:

    allowance = FLEET_SHARE_CEILING_PCT * FLEET_GRU_ALLOWANCE_FRACTION

FLEET_SHARE_CEILING_PCT already IS this instance's share of the account's real headroom
(run_member.sh computes it directly from maxx's own headroom_fraction gauge x
FLEET_SHARE_FRACTION -- gh#1215, not cross-instance-coordinated: two instances sharing one
maxx account each spend up to their own share independently). Taking gru's fraction OF that
yields both nested percentages and leaves 1-F of the instance's slice for everyone else --
which is what the dial always claimed to mean.

FAILS OPEN, same law as maxx_reader.py: no ceiling exported (meter unreadable, or the
operator never set FLEET_SHARE_FRACTION) means this prints nothing and
gru falls back to its own documented conservative default. An absent reading must only ever
be usable to CONSERVE. A ceiling of exactly 0.0 is a real, honest answer and is printed as
0.0000 -- distinct from "no reading".
"""
from __future__ import annotations

import os
import sys

DEFAULT_FRACTION = 0.70

# UNCALIBRATED METER (2026-09-25). maxx_reader answers a never-billed account with
# headroom_fraction=1.0 / label calibrating_unbilled -- right for ranking the account pool, but
# run_member.sh turns it into FLEET_SHARE_CEILING_PCT = 1.0 * 0.6 * 100 = 60, and this printed
# 36.0000: a third of a week in one hour. That is no reading, not a real one. Fall back to a
# conservative FIXED allowance instead: the even-pace hour (100% of the week / 168 hours) times
# the same two nested fractions -- 0.2143 at 0.6 x 0.6. Operator override:
# FLEET_UNCALIBRATED_ALLOWANCE_PCT. Priced by cost_bridge's unit, that still packs several items.
UNCALIBRATED_LABELS = {"calibrating_unbilled"}
EVEN_PACE_HOURLY_PCT = 100.0 / 168.0


def _fraction(raw, default: float) -> float:
    try:
        f = float(raw) if str(raw or "").strip() else default
    except ValueError:
        f = default
    return max(0.0, min(f, 1.0))


def uncalibrated_allowance(share_raw: str | None, fraction_raw: str | None,
                           override_raw: str | None = None) -> str:
    """The fixed allowance for an uncalibrated meter, formatted like compute()."""
    try:
        if str(override_raw or "").strip() and float(override_raw) >= 0:
            return f"{float(override_raw):.4f}"
    except ValueError:
        pass
    pct = EVEN_PACE_HOURLY_PCT * _fraction(share_raw, 1.0) * _fraction(fraction_raw, DEFAULT_FRACTION)
    return f"{pct:.4f}"


def compute(ceiling_pct: str | None, fraction_raw: str | None, label: str | None = None,
            share_raw: str | None = None, override_raw: str | None = None) -> str:
    """Return the allowance as a formatted string, or "" when there is no trustworthy input."""
    if (label or "").strip() in UNCALIBRATED_LABELS:
        return uncalibrated_allowance(share_raw, fraction_raw, override_raw)
    if ceiling_pct is None or str(ceiling_pct).strip() == "":
        return ""   # fail open: no ceiling => caller keeps its own fallback
    try:
        ceiling = float(ceiling_pct)
    except ValueError:
        return ""
    if ceiling < 0:
        return ""

    # An operator typo (1.5, or a negative) must never hand gru more than the instance's own
    # slice, nor a negative allowance. Clamp rather than fail: the ceiling is still a real,
    # safe number and refusing it entirely would idle the fleet over a config slip.
    fraction = _fraction(fraction_raw, DEFAULT_FRACTION)

    return f"{ceiling * fraction:.4f}"


def main(argv: list[str]) -> int:
    ceiling = argv[1] if len(argv) > 1 else os.environ.get("FLEET_SHARE_CEILING_PCT")
    fraction = os.environ.get("FLEET_GRU_ALLOWANCE_FRACTION")
    label = os.environ.get("FLEET_MAXX_LABEL")
    print(compute(ceiling, fraction, label, os.environ.get("FLEET_SHARE_FRACTION"),
                  os.environ.get("FLEET_UNCALIBRATED_ALLOWANCE_PCT")))
    if (label or "").strip() in UNCALIBRATED_LABELS:
        print(f"gru_allowance: maxx meter uncalibrated ({label}); fixed even-pace allowance, "
              f"not the {ceiling} ceiling", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
