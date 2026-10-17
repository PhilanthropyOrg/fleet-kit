# dumbledore charter: history and rule ledger

This file is NOT loaded into dumbledore's prompt. `members/dumbledore/dumbledore.md` says what
to do now; this file keeps why. The charter cites it as `H§n`. The old text is in git:
`git show 2fb3f83:members/dumbledore/dumbledore.md`.

## History

**H§1. CI levers priced 10-11.** 3 of 4 passes to 10-11 spent 10+ turns re-pricing levers
already set. Known then: deploy wait_timer=90 is live, cancelled deploys bill 0; OPS:
AUTO-MERGE ~55 min/d; minion PR CI 12-24 min vs Reif's 4-8 (batch size; a `tests/conftest.py`
touch runs the full suite); review-before-ready ships in `minion_checkpoint.py ready`.
Hand-built waste queries cost 35-102 turns a pass (10-04), hence the one-call block in step 3.

**H§2. Pass counts are not bets.** 7 of the 10 misses in #37-#47 (09-30..10-03) bet on pass
counts, which cron lines in `entrypoint.sh`, pushes, wakes and gru set, not a charter; the
other 3 asked a per-pass number to go UP, which a charter allows but does not cause. Four
minion rows due within 24h (#38/#39/#44/#45) confounded each other; three missed. Fleet-internal
bets alone left the claim funnel unbet through bet #96 (10-12), hence the open outcome bet.
