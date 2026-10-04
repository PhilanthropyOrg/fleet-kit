# gru charter: history and rule ledger

This file is NOT loaded into gru's prompt. `run_member.sh` loads only the file named by
`llm.prompt_file` in `members/gru/gru.fleet.json` (`gru.md`), and this file lives outside
`members/` so nothing that globs `members/*/*.md` picks it up either.

`members/gru/gru.md` says what gru must do now. This file keeps the story of why: the
incidents, dates, quotes and numbers that used to sit inline. The charter cites it as `H§n`.
The full old text is in git: `git show 39f06b7:members/gru/gru.md`.

## History

**H§1. The forced 11-item checklist.** 2026-08-23, on dont-shoot-the-messenger: with no
forced plan, a real pass spent its whole budget on steps 1-6, never reached the report step,
and landed `reported_nothing` despite real work.

**H§1b. Three grouped tasks, not eleven (2026-10-04, dumbledore).** The last 25 gru passes made
34 tool calls on average, 14.6 of them ToolSearch/TaskCreate/TaskUpdate bookkeeping, while gru's
avg_turns rose 11.9 (09-30) to 32 (10-04). The plan H§1 needs survives as three tasks; the per-step
create/update pairs do not. If `reported_nothing` returns for gru, H§1 is back and this reverts.

**H§2. Red PRs before new builds (2026-09-25).** #7975, #7982 and #7986 (all
fleet:reif-priority builds) sat red for hours while three passes in a row went straight to
step 2a and built more. The 60-minute rule is the red half of the merge-stall alarm, which
only sees green PRs. Kit PRs were added 2026-09-29: four fleet-kit PRs sat stuck 2-20h
because nothing looked at that repo.

**H§3. Detached dispatch, never backgrounded.** 2026-09-25: gru ended its turn at 20:25:57
and every fixer it had backgrounded was killed at 20:26:06 (exit 143), #7982's with its fix
half done. 2026-09-26: gru ended its turn at 13:45:21 and minion #7938, resuming checkpoint
#8134, was SIGTERMed at 13:45:27. Those kills are what benched five reif-priority items as
dead ends (fk#1313). `dispatch_fixer.sh` (#1303) and `dispatch_member.sh` are the fix.

**H§4. Red draft PRs get resumed (2026-09-29).** `red_prs.py` used to skip every draft, so
checkpoints #8531/#8550/#8553/#8603/#8604 sat red 14-22h while their items waited their turn
in the tiers. The script records each `resume` listing, does not re-send the same red content
for 45 minutes, and moves it to `exhausted` after 3 tries. The stop hook keeps a resumed
minion on the PR until CI is green.

**H§5. Claims are leases (2026-09-25).** A dead minion batch left #7937-#7942, #7948 and
#7950 claimed for 12 hours, and every pass reported "no open item passed both gates". Those
12 hours of "no open item" were really "8 items held by a dead claim", and nobody could tell.
Hence the release sweep at pass start and the `Skipped as claimed:` report line.

**H§6. Run the script, never do the arithmetic.** Across 69 real fanouts, N wandered 1-4
with no relationship to headroom when passes re-derived it from prose. See
`scripts/gru_allowance.py`'s header.

**H§7. The allowance formula.** maxx applies both buffers first (`weekly_max` 0.925 of the
week, `per_diem_use` 0.95 of the day). Two nested shares multiply:
`FLEET_SHARE_FRACTION` (this instance's share of the account, e.g. 0.20) gives
`FLEET_SHARE_CEILING_PCT` (exported by run_member.sh from maxx's headroom gauge, gh#1215),
and `FLEET_GRU_ALLOWANCE_FRACTION` (e.g. 0.75) is gru's share of that: 0.0142 x 0.75 = 0.0106.
A prior version used `min()` and fed it consumption; gru silently claimed the instance's
whole slice while every other member's dial did nothing (Reif, 2026-09-02). The ceiling is
not coordinated across instances: an earlier version subtracted other instances' live
reservations, needed pacing fields a fresh account lacks, and zeroed the whole instance.
Accepted tradeoff (Reif, 2026-09-22: "maxx didnt work as a pooled usage engine, but it does
work as a gas guage"). Stale-pin 0.0 reading with `label: ok`: 2026-08-26 incident. On an
uncalibrated meter the script used to slice the meaningless 1.0 gauge (2026-09-25: it printed
36.0000, a third of a week in one hour); it now prints 100/168 x FLEET_SHARE_FRACTION x
FLEET_GRU_ALLOWANCE_FRACTION, or `FLEET_UNCALIBRATED_ALLOWANCE_PCT`.

**H§8. Account readiness.** 2026-08-25: real passes spawned minions that died on
`ALL_ACCOUNTS_EXHAUSTED` with zero work done. `ready` used to cap N at the live account
count; wrong, because `account_pool.sh` is a sequential failover chain. Decline rate falls
as N rises (48% declined at N=1, 17% at N=4 across 69 real fanouts).

**H§9. Drought short-circuit.** 12 passes re-deriving `n=0` cost about $6.90 for nothing
(2026-09-03/04). gh#361 is the standing budget tracking issue; gh#568 added the structured
ask because a label alone carries no `why`/`unblocks`/`proposed`.

**H§10. Reif-priority is first in line, never the whole list.** gh#5278: four passes each
spent a full turn budget re-confirming a 100%-blocked reif-priority tier, reported `quiet`,
and starved `fleet:priority-high` work (including a revenue-critical fix, gh#831).
2026-09-28 21:16 CDT: 2-3 reif-priority survivors were the entire pack, n=2 at 40% of the
hour, with 221 unblocked items open.

**H§11. Acceptance criteria are not gru's to narrow.** #7220 (philanthropy#8475): gru
decided 2 of Reif's required checks didn't need building, dispatched the rest as the whole
spec, and the issue closed looking done. Scope calls made visibly: Reif 2026-09-28,
persona_law.md §2b.

**H§12. Draft PRs are resumable.** 2026-09-26: gru dropped #7939 and #7950 as "owned by
open draft PRs #8135/#8133", so their work sat unresumed. 2026-09-27: #8286 (epic #8177) and
#8360 (dead-end #7939) were left open after a gate drop and looped watchdog -> jefe ->
the-fixer for hours (jefe msg#134).

**H§13. Fix-issues for red PRs outrank tiers (2026-09-19).** nonprofit-atlas #6914 went
red on CI's UI gate, judge-judy filed #6915 as priority-high, marie re-ranked it medium, and
gru drained the high tier for three hours while a one-line fix sat unclaimed until a human
pushed it.

**H§14. needs-human-op filter.** gh#3920 found #2195 re-claimed and re-spawned 15+ times
because the filter was missing. `fleet:needs-prod-access` was added in philanthropy#8218.

**H§15. Oldest first within a tier.** gh#360: a build-ready high-priority spec sat
unclaimed 10 days at position 22 of 23 in its tier because `gh issue list` returns
newest-first.

**H§16. Dead-end gate.** gh#64: without it a chronically blocked item is reclaimed and
respawned every hour. Threshold 3 in 14 days is a reasoned default (`claim_history.py`
docstring). Reif, 2026-09-26: a dead-end claim is ONLY a minion run whose report said
`Blocked: #<n> <reason>`; kills (rc=143), timeouts (rc=124), infra statuses, checkpoint
drafts and Part-of PRs never count. Stalls: philanthropy#7942 was resumed on draft #8194
about 40 times in two days, each pass re-verifying the same finished half. Visible drops:
gh#5934, thirty `fleet:priority-high` items sat invisibly blocked on 2026-09-14. The
crowd-out rule that once made gate order load-bearing (gh#593) was removed in fk#1191.

**H§17. Never a silent drop (philanthropy#8215).** Reif, 2026-09-26: "so gru passes on
something with no quality label and then never alerts anyone?" 26 of 30 priority-high items
were dropped that day into gru.log alone. `gate_drops.py` makes every drop land somewhere;
the console's "Gate drops" tile counts the last 6h; the ask reaches Reif in the next brief.

**H§18. Fill the gap, and the Vision-link gate's past.** Reif, 2026-09-26: "can we be not
retarded, and not get caught up on stupid stuff like this": 26 items sat dropped for days
(5 of them his own `fleet:reif-priority` asks) waiting on marie's 4-hourly pass to write two
lines. Gate history: gh#525; registered KR ids only since Reif 2026-09-16 ("KR2 -- messages"
passed while no such KR existed); marie's PRD template lacked the line before PR#587 (gh#588
backfilled 18 issues); marie's `Vision-link:`-only comment is gh#4597; maintenance items stopped
being crowded out, and PRDs stopped being high-tier only, in fk#1191.

**H§19. Quality gate and VP review.** fk#649/#651, Reif 2026-09-07: "I'd rather us push
less code but better features" (105 product PRs merged that day against issues with no
stated bar). Reif 2026-09-08: "It's appropriate to have our system decide what is acceptable
instead of having a human decide it. Just say: OK, I'm a Google VP, would this pass?"
`vp_due.sh` runs on cron (default every 15 minutes, `FLEET_VP_DUE_CADENCE`). vp.md caps
world-class redo cycles at three Not-yet rounds.

**H§20. Plan bets and the cheap-item head start.** gh#572 (plan files from #570/#571; order
without a plan is PR#536's). gh#5211: a 2-file, complexity-2 checkout fix sat at position 27
of its tier with zero minion runs ever, because nothing weighted complexity.

**H§21. Pricing the hour.** `cost_bridge.py` (gh#4020 / fleet-kit#260) spreads the pass's
allowance across the last 2h of real minion `cost_usd`; with fewer than 5 runs it prices a
median item from the last 30 days, capped at `allowance_pct / 4` (2026-09-25: one recent run
priced a single item as the whole hour and a pass with 9 eligible items built 1). Items-per-run
floor, 2026-09-24: a minion run is almost all fixed overhead (1 item $2.75/794s, 3 items
$3.41/1312s).

**H§22. Anti-starvation floor.** gh#360 fixed candidate order; this fixes progress.
2026-09-05, gh#427: a complexity-3 item sat first-in-`skipped` for three straight passes
while smaller, days-younger items kept getting chosen. Read `runs.jsonl` because `gru.log`
truncates every line to 500 chars (`_text_preview()`, `scripts/stream_log.py`) and `chosen`
is serialized before `skipped`.

**H§23. Reservation.** `reserved_pct` was silently 0 on every pass: the formula subtracted
it but nothing wrote it, so correctly capped hourly passes compounded into an unsustainable
day. An unreleased lease double-holds the hour until its TTL clears.

**H§24. One status comment.** philanthropy#7942 reached 96 comments, 42 of them
`claimed-by:`, and every later agent re-read them all. Claiming in gru's own context, one
item at a time, is what removes the claim race.

**H§25. Batching.** 2026-09-14, Reif: batch minions, each PR is one full CI run. 2026-09-26:
a pass typed `--target-items 8` on an instance set to 3 and packed #7938 #7939 #7941 #7950
(four complexity-5 items) into ONE minion; it hit its 5400s timeout with no PR, about 90
minutes lost, same as 09-25 09:19. Reif, 2026-09-29: at least 10 issues per PR ("a model,
not a human, fixes what a big PR breaks"). One batch per `area:` module per pass:
philanthropy#8218. `--turn-budget 0` with the env target (2026-09-24) makes each batch's
budget `unit_turns * N`.

**H§26. Standing lanes.** Reif 2026-09-23: "these are likely big enough that one of the
agents needs to be always working on it... in 5 days will pSEO still be important".

**H§27. Lane coverage.** Folded from datta in fk#1195 (datta ran hourly as its own member).
The `lane` column replaced keyword matching after a real mis-attribution. gh#339: a lane
with no surface kept winning worst-first on staleness. gh#447: zeroing UNEXAMINED meant no
new run was ever recorded, so the down-rank could never lift itself. gh#530 (judge-judy):
"rank, combine, then cap" dropped the probe on exactly the passes where ranking already
filled the cap. gh#392 / gh#497: the reconfirmation hold, and its STALE/BREACHED guard. No
fleet-wide KPI noise threshold was defined as of 2026-09-05. gh#451: the `STRUCTURAL-N/A`
marker.

Other references kept inline in the charter: fleet-kit#784 (INTENT.md), gh#152 (`wait $PID`),
gh#425 (PR search noise), fk#1127 / fk#1202 (fold-in), fk#1195 (asks answered as reif-via-M;
new asks of most classes already file as notices, authority.py's default since 2026-09-28).

## Rule ledger

Verdicts: KEEP (still needed, restated), MERGE (said twice, kept once), CODE (a script now
guarantees it; at most one short line kept), SUPERSEDED (a later rule contradicts it), DEAD
(points at something gone), HISTORY (the story of why; moved here).

| # | Rule or text in the old charter | Verdict | Note |
|---|---|---|---|
| 1 | Provenance paragraph (split from single-worker 2026-08-21) | HISTORY | one line kept: never build, never rank |
| 2 | Intent first: INTENT.md outranks marie's ranking | KEEP | verbatim |
| 3 | TaskCreate with 11 items, steps 0-10 | KEEP | pinned by test_gru_red_prs_first |
| 4 | "the fanout script that used to spawn many of you" | HISTORY | |
| 5 | Step 0: run `red_prs.py due` every pass, even on a drought | KEEP | |
| 6 | Step 0: `due` ordered and capped at FLEET_GRU_MAX_FIXERS | CODE | red_prs.py:330 |
| 7 | Step 0: one `dispatch_fixer.sh` call for every entry | KEEP | |
| 8 | Step 0: kit PRs via `--kit` and `kit:<PR>` | KEEP | |
| 9 | Step 0: detached, never backgrounded, never run_member.sh directly | KEEP | |
| 10 | Step 0: review findings are included | KEEP | pinned |
| 11 | Step 0: read each dispatched PR's state before reporting | KEEP | |
| 12 | Step 0: do not re-batch a due PR's items | KEEP | |
| 13 | Step 0: `held` no re-send, `exhausted` named, `error` is blind | KEEP | |
| 14 | Step 0: `resume` drafts get a minion; claim then dispatch | KEEP | cross-ref fixed: old text said "as step 3 does"; dispatch is step 5 |
| 15 | Step 0: 45-minute hold and 3 tries for resume | CODE | red_prs.py verdicts (:218, :251-255) |
| 16 | Step 0: `superseded` drafts get closed | KEEP | |
| 17 | Step 0: read `stale_claims.py last`; re-run release if stale | KEEP | |
| 18 | Step 0: build on the PR a released item's comment names | KEEP | |
| 19 | Step 1: percent of week, never dollars | KEEP | |
| 20 | Step 1: maxx buffer values 0.925 / 0.95 | HISTORY | H§7 |
| 21 | Step 1: `headroom_fraction` is not the allowance; 0.0 check | KEEP | |
| 22 | Step 1: run `gru_allowance.py`, never compute | KEEP | |
| 23 | Step 1: multiply, never min; headroom, never consumption | CODE | gru_allowance.py does it; one line kept (pinned by selftest) |
| 24 | Step 1: worked example and cross-instance explanation | HISTORY | H§7 |
| 25 | Step 1: "the other eight members (marie, jefe, ...)" | DEAD | says eight, lists seven; roomba has since been folded into marie |
| 26 | Step 1: uncalibrated meter formula | CODE | gru_allowance.py:55-107; kept: quote the stderr line |
| 27 | Step 1: verify a dial change moves the number | KEEP | |
| 28 | Step 1: an unspent hour is gone | KEEP | |
| 29 | Step 1: fail open on an unreadable meter | KEEP | |
| 30 | Step 1: you own which items, never how many | KEEP | |
| 31 | Step 1: account readiness; ready=0 hard stop | KEEP | |
| 32 | Step 1: `ready` is not a cap on N | KEEP | the old cap it replaced is HISTORY |
| 33 | Step 1: drought check, tracking-issue comment, ask, end pass | KEEP | |
| 34 | Step 2a: reif-priority read, filters, front of `--items` | KEEP | |
| 35 | Step 2a: "it IS this pass's work, full allowance" | SUPERSEDED | by "first in line, never the whole list" (2026-09-28), same paragraph |
| 36 | Step 2a: blocked tier falls through to 2b | KEEP | |
| 37 | Step 2a: never close a reif-priority issue | KEEP | |
| 38 | Step 2a: never narrow acceptance criteria; Scope call + ask | KEEP | |
| 39 | Step 2a: draft PRs are resumable, not owned | KEEP | |
| 40 | Step 2a: close a gate-dropped item's draft with one line | KEEP | |
| 41 | Step 2a-bis: fix issues for red PRs go first; three PR states | KEEP | |
| 42 | Step 2a-bis: "Only when no such fix survives do you read 2b" | SUPERSEDED | by 2a "2a-bis and 2b's tiers follow in the same list" and step 3's tier loop; test_gru_throughput forbids "skip 2b's query entirely" |
| 43 | Step 2b: tier query, sorted by createdAt | KEEP | |
| 44 | Step 2b: filter claimed / needs-human-op / needs-prod-access | KEEP | |
| 45 | Step 2b: append medium then low until the hour is funded | KEEP | |
| 46 | Step 2b: choose from marie's ranking; unlabeled is lowest | KEEP | |
| 47 | Step 2b: "Three filters, fixed order" | MERGE | there are four gates; one sentence now names all four |
| 48 | Step 2b: never silently drop; name by number and reason | MERGE | said four times; kept once |
| 49 | Dead-end: run `claim_history.py`; BLOCKED drops | KEEP | |
| 50 | Dead-end: what counts (Blocked: lines only, stalls, threshold) | CODE | claim_history.py:11-21, :156 |
| 51 | Dead-end: `dead_end_label.py` on BLOCKED; clear on ok | KEEP | |
| 52 | Dead-end: hand removal is a retry | KEEP | |
| 53 | Dead-end: `Un-parked:` comment exception | KEEP | pinned by test_zero_human_waits |
| 54 | Vision-link: run `gate_drops.py run`, never the cores | KEEP | |
| 55 | Vision-link: pass conditions | CODE | vision_link_gate.py; two lines kept because gru writes the missing line itself |
| 56 | Vision-link: template / backfill history | HISTORY | H§18 |
| 57 | Vision-link: a missing line after marie's sweep is a gap to flag | MERGE | covered by needs-spec + fill-the-gap |
| 58 | gate_drops: what `fixed` / `needs-spec` / `by-design` mean | KEEP | shortened |
| 59 | gate_drops: write candidates to a file | KEEP | |
| 60 | Fill the gap yourself, then re-gate | KEEP | |
| 61 | Quality gate conditions | CODE | quality_gate.py; kept short for the same reason as 55 |
| 62 | vp: due conditions | CODE | vp_due.py on cron; one sentence kept |
| 63 | vp: spawn only when due and `vp_due.py` agrees; one per item | KEEP | |
| 64 | vp: never file a decision ask for design/acceptance | KEEP | |
| 65 | "don't fall back to an ungated tier" | KEEP | |
| 66 | Oldest-createdAt-first within a tier | KEEP | |
| 67 | Plan bets: `plan_rank.py`, use `ranked`, quote the inactive line | KEEP | |
| 68 | Plan bets: "(#570/#571, when either has landed)" | DEAD | both landed; docs/plan/ exists |
| 69 | Collect complexity (default 5), area, mega is one item | KEEP | area sentence pinned |
| 70 | Complexity-1/2 head start, max 2 | KEEP | |
| 71 | 2d: fold items dispatched directly with `--item --onto-pr` | KEEP | |
| 72 | 2d: "still one claim per item first (step 4's claim, done here)" | SUPERSEDED | by "never pre-claim it by hand" (fk#1202), same step |
| 73 | Step 3: calibrate with cost_bridge, pack with fanout | KEEP | |
| 74 | Step 3: how cost_bridge prices (2h window, 30-day fallback, caps) | CODE | cost_bridge.py; kept: quote its stderr line |
| 75 | Step 3: marie's order, packer never reorders | KEEP | |
| 76 | Step 3: items-per-run floor and when not to use it | KEEP | |
| 77 | Step 3: candidates_exhausted loop | KEEP | |
| 78 | Step 3: quote the JSON; never substitute a number | KEEP | field list dropped (the JSON is quoted whole) |
| 79 | Step 3: anti-starvation floor | KEEP | |
| 80 | Step 3: "never force an item not front-of-skipped 3 passes" | MERGE | it is the trigger condition restated |
| 81 | 3a: reserve est_spend_pct, keep lease_id | KEEP | see "least sure" in the PR |
| 82 | 3b: compare last estimate to actual; tell marie on a systematic miss | KEEP | |
| 83 | 3b: never adjust the estimate yourself | KEEP | |
| 84 | 3b: "Lower tiers fill whatever room..." | MERGE | duplicate of 45 |
| 85 | Step 4: claim serially with `claim-item`; release edits in place | KEEP | |
| 86 | Step 4: never a new comment for claim news | KEEP | |
| 87 | Step 5: one minion per batch; size is an output | KEEP | |
| 88 | Step 5: cost_bridge --batch-turns; cold-start --unit-turns | KEEP | |
| 89 | Step 5: never type --target-items / --solo-complexity-floor / --timeout-s | KEEP | |
| 90 | Step 5: cap, minimum 10, no solo by size, timeout ceiling | CODE | fanout.py:367-376, :454-469; run_member.sh refuses an over-cap minion |
| 91 | Step 5: "why a big item got its own batch" | SUPERSEDED | by "no item goes solo by size" (2026-09-29); solo floor default is 11 = none (fanout.py:459) |
| 92 | Step 5: release and name `deferred`; `over_timeout` named | KEEP | |
| 93 | Step 5: timed-out item is re-dispatched; its draft is its own | MERGE | with 39 |
| 94 | Step 5: how pack_batches groups areas | CODE | fanout.py docstring |
| 95 | Step 5: dispatch_member.sh, foreground, exact numbers, record pid | KEEP | |
| 96 | Step 6: `--wait`, re-run, report `running (pid N)` | KEEP | |
| 97 | Step 6: never `wait $PID`, never end to wait for a notification | KEEP | |
| 98 | Step 6: release the lease | KEEP | |
| 99 | Step 7: read runs.jsonl by run_id; local PR match, not search | KEEP | |
| 100 | Step 7: one combined report; failures named; plan bet per item | KEEP | |
| 101 | Step 8: never build, never re-rank; flag for marie | KEEP | |
| 102 | 8b: standing lanes, build + find + report line | KEEP | |
| 103 | Step 9: dispatcher/worker split; never analyse a lane | KEEP | |
| 104 | Step 9: "datta used to run hourly..." | HISTORY | H§27 |
| 105 | 9a: read KPIs, never compute | KEEP | |
| 106 | 9b: STALE / BREACHED / UNEXAMINED, `lane` column | KEEP | |
| 107 | 9b: structural-N/A streak down-rank | KEEP | |
| 108 | 9b: frozen-lane probe, exempt from the cap | KEEP | |
| 109 | 9b: why combine-then-truncate fails | HISTORY | H§27 |
| 110 | 9b: gh#392 reconfirmation hold, six steps | KEEP | |
| 111 | 9c: fewer nerds than lanes is normal; flat cap | KEEP | |
| 112 | 9d / 9e: dispatch nerd detached; wait; read real result | KEEP | |
| 113 | Step 10: answer open asks except credential/money | KEEP | |
| 114 | Step 10: "INTENT.md (step 0)" | DEAD | INTENT.md is not in step 0; pointer corrected |
| 115 | Step 10: why few such asks exist now | HISTORY | |
| 116 | Report: coverage audit trail | KEEP | |
| 117 | Report: `Skipped as claimed:` line | KEEP | |
| 118 | Report: `Report:` block then Outcome/Evidence lines | KEEP | shortened; run_member.sh also appends §10b |

Counts: KEEP 86, MERGE 6, CODE 11, SUPERSEDED 4, DEAD 3, HISTORY 8 (118 rows).

## Contradictions found

1. **2a, same paragraph.** "it IS this pass's work, full allowance" against "this tier is
   first in line, never the whole list". The later rule (2026-09-28) wins.
2. **2a-bis against 2a and step 3.** "Only when no such open-PR fix survives do you read 2b"
   against "2a-bis and 2b's tiers follow them in the same list" and the tier loop. The
   fill-the-hour rule wins; a test already forbids the old wording.
3. **2d, same step.** "still one claim per item first" against "never pre-claim it by hand"
   (fk#1202). The later rule wins.
4. **Step 5.** "why a big item got its own batch" against "no item goes solo by size"
   (2026-09-29). The later rule wins.
5. **Step 2b.** "Three filters" while the text runs four gates plus two label filters.
6. **Step 0.** "exactly as step 3 does" for dispatch, which is step 5.
7. **Not fixed here (other files):** `members/gru/gru.fleet.json` still says "puts every
   complexity>=5 item in its own minion", "spawn ONE minion per batch in the background" and
   "with FLEET_RUN_NOW=1", all three against the charter. And `FLEET_MINION_TARGET_ITEMS=8`
   (hard cap per batch) sits under `FLEET_MINION_MIN_ITEMS=10` (fewest per PR), so no batch
   can reach the minimum on default dials; fanout.py lets them go anyway.
