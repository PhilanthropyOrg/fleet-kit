# nerd charter: history and rule ledger

This file is NOT loaded into nerd's prompt. `run_member.sh` loads only the file named by
`llm.prompt_file` in `members/nerd/nerd.fleet.json` (`nerd.md`), and this file lives outside
`members/`.

`members/nerd/nerd.md` says what nerd must do now. This file keeps the story of why, and the
dated snapshot numbers that used to sit inline. The charter cites it as `H§n`. The full old
text is in git: `git show 39f06b7:members/nerd/nerd.md`.

## History

**H§1. Acceptance and Vision-link endings.** gru's gate reads a `Vision-link:` line only
when it starts a line. `Lane: datadog. Vision-link: okr.traffic` read as missing (#8278,
#8313); #8345 and #8275 had no Acceptance at all. Such issues were born `fleet:needs-spec`.

**H§2. Check open work before filing.** gh#419 and gh#512 both proposed the same
`member_liveness_check.sh` dead-man's-switch 50 minutes apart. #512 even cited #419 in its
"Refs" line, but nobody checked that #419 already had an open PR (opened 32 minutes before
#512 was filed).

**H§3. UNCAPPED.** Measured on the source fleet: 108 of 211 scout passes filed ZERO, and
one lane filed 12 items across 211 passes. "QUIET, nothing new" with no evidence line is
indistinguishable from looking at the same dashboard and giving up.

**H§4. Operator-flagged threads.** The charter once named a "half-way checkpoint", and it
got read as an allowance. On 2026-09-10 all six nerd passes obeyed it and still opened their
self-critique with "I spent the first ~15% / ~25% / ~35% / close to half of the budget
re-verifying the operator-flagged thread"; three proposed the cheaper move themselves ("I
could have checked just the label state first (30 seconds)").

**H§5. Why prod-runtime exists (Reif, 2026-09-24).** The product repo's old devops lane
watched "atlas-serve prod system: uptime/Postgres/deploy delivery/cost", and it died when
this fleet replaced the in-repo lanes. Nobody inherited it, and a human found by hand what
any pass could have read in seconds: 157/200 DB connections (141 idle), the app on the admin
role, no statement_timeout, a 178s query, a full-scan vector search at 6.3M calls, and
ingests failing silently. `prod_runtime.py` checks: pg_stat_statements top-N by total and by
max time, connections vs max_connections by state, long-running and idle-in-transaction
sessions, locks, seq scans on big tables, statement_timeout per role, bulk writes on the
primary, load/memory/disk, journal errors, service restarts, cron failures and drift against
the repo's `scripts/box/crontab`, and ingest freshness. Its own cron runs every 3h.

### Snapshot numbers moved out of the prompt (measured on the product, 2026-08-26)

These were true on the day they were written and went stale inside the prompt.

- Growth readings: pages surfaced 98,171 (-6,315); shards fetched 339/339 and 83/83; ~27%
  indexed (n=15); Page Indexing buckets 3.7M discovered-not-indexed, 1.68M noindex.
- Page types already shipped: `/990/nonprofits/in/<state>` and `/in/<state>/<city>` (both
  200, ~119 internal links each), `/990/who-funds/`, `/990/funders-for/`, `/990/grants-by/`,
  `/990/foundations-funding/`, `/990/salaries/`, `/990/report/`, `/990/people/`. Missing
  (404): `/in/<state>/<category>`, `/in/<state>/<city>/<category>`, and a similar-orgs page.
- Search: `?q=red+cross` returned American National Red Cross first, then ICRC, then
  chapter/PTO noise, so the lane's work is in the tail. Officers are tens of millions of rows.
- UI: the front page carried ~94 follow hooks and 2 login links. Front page ships PostHog,
  gtag and Clarity.
- Devops baseline: `/990` 200 in 0.45s, search 0.79s, `/990/api/health` 200 in 0.26s
  (nonprofit-atlas only). fleet-kit's own target `http://localhost:8571/` answered in ~0.03s
  (2026-09-03, gh#143).
- Browser: playwright + headless chromium added to the image 2026-08-26, verified loading
  the live site and reading its `<h1>`.
- Google's `sitemaps.list` `indexed` field has been deprecated since 2019 and reports ~0%.
- Credentials were verified live on the prod box 2026-08-26.

### fleet-kit as the target repo

- growth: fleet-kit is private with no stars or forks and no crawlable surface. An
  onboarding-path check was tried as a stand-in KPI and ruled out (it stayed clean and gave a
  later pass nothing to act on). The `STRUCTURAL-N/A: ` marker is gh#451; gru's down-rank is
  gh#339.
- ui findings made under this framing: gh#233 (the dial editor wrote `fleet.env` with no
  input validation), gh#166 (status dot mislabels a running member as "disabled").
- datadog findings: gh#437 (`runs_summary()` double-counts provisional "started" rows),
  gh#485 (`/status` has no tile for `fleet.db` sync freshness).
- revenue: confirmed 2026-08-29, no billing or signup code anywhere in the repo.

## Rule ledger

Verdicts as in `gru.md` next to this file.

| # | Rule or text in the old charter | Verdict | Note |
|---|---|---|---|
| 1 | One lane, one pass, findings with evidence | KEEP | |
| 2 | The one question: massive user value; the test for a finding | KEEP | shortened |
| 3 | The KPI is a proxy; disagreement is the finding | KEEP | pinned |
| 4 | Small and certain vs big and vague | KEEP | |
| 5 | Rank by user value; marie ranks the rest | KEEP | |
| 6 | Issue bodies end with `## Acceptance` and a `Vision-link:` line | KEEP | incident numbers to H§1 |
| 7 | Work only `lane=<name>`; file out-of-lane finds and say so | KEEP | |
| 8 | TaskCreate with the 5 items | KEEP | verbatim |
| 9 | Checklist half first, every time | KEEP | |
| 10 | Exploration half: the checklist is a floor | KEEP | |
| 11 | Operator-flagged thread: check state first, ~10% of budget | KEEP | story to H§4 |
| 12 | Discovery 1: read own history from fleet.db; `lane` column | KEEP | |
| 13 | A finding nobody builds: stop, or re-file once | KEEP | |
| 14 | Discovery 2: read merged PRs | KEEP | |
| 15 | Check `--state open` before filing; stop on claimed / open PR | KEEP | story to H§2 |
| 16 | Discovery 3: demand, adjacent build, competitor | KEEP | |
| 17 | Discovery 4: judge what is buildable | KEEP | |
| 18 | Every finding states its user value in one line | MERGE | said in three places; kept once under Filing |
| 19 | Two prompts: untouched surface, label vs query | MERGE | folded into discovery 3; label-vs-query also stays in lens |
| 20 | UNCAPPED law; file as displacing at cap | KEEP | numbers to H§3 |
| 21 | Evidence or it did not happen; `unverified:` | MERGE | said twice ("Evidence" and "Never invent a number"); kept once |
| 22 | KPI / guardrail / cheat table | KEEP | cheat text shortened |
| 23 | Read your KPI, never compute it; every item names its KPI | KEEP | |
| 24 | Check access before planning; never echo a secret | KEEP | |
| 25 | Credentials table | KEEP | "verified live 2026-08-26" to history |
| 26 | SA keys are paths; google-auth + urllib only | KEEP | |
| 27 | A missing credential is a finding | KEEP | |
| 28 | Its example names `GOOGLE_CLIENT_ID` | DEAD | no such variable; the table and the next paragraph say service-account keys, not OAuth client ids |
| 29 | No-API numbers need a human read | KEEP | |
| 30 | growth: MAXIMIZE INDEXED PAGES | KEEP | |
| 31 | growth on fleet-kit: N/A with `STRUCTURAL-N/A: ` | KEEP | one shared N/A paragraph; marker still inside each lane's section (pinned) |
| 32 | growth: four numbers table | KEEP | dated readings column to history |
| 33 | growth: never file a regression on sample movement; 10% noise | KEEP | |
| 34 | growth 1: read the Page Indexing report in a browser | KEEP | |
| 35 | Playwright snippet, `--no-sandbox` | KEEP | moved to Access (every lane uses it); minion.md points at it |
| 36 | "Search Console needs a login the box may not hold" | MERGE | duplicate of 27 |
| 37 | growth: sort deliberate from accidental noindex | KEEP | |
| 38 | growth: discovered-not-indexed is not a discovery problem | KEEP | bucket sizes to history |
| 39 | growth 2: build to demand; sitemap AND internal links | KEEP | |
| 40 | growth 2: ALREADY SHIPPED route list | HISTORY | a 2026-08-26 snapshot; rule kept: check what exists first |
| 41 | growth 2: CONFIRMED MISSING category x geo, similar-orgs | KEEP | now says "when last verified, re-check before filing" |
| 42 | growth 3: guard what already ranks; verify through Google's eyes | KEEP | |
| 43 | growth: thin-page flood is a BREACH | MERGE | in the cheat table |
| 44 | searchquality: judge the human outcome; tail; real telemetry | KEEP | "verified 2026-08-26" to history |
| 45 | searchquality on fleet-kit: gh#193, else N/A | KEEP | |
| 46 | ui: use the browser; what to check | KEEP | hook counts to history |
| 47 | ui on fleet-kit: fleet_view surface; ui vs lens | KEEP | past findings to history |
| 48 | datadog: metric integrity; what to check | KEEP | |
| 49 | datadog on fleet-kit: fleet.db and runs.jsonl are the spine | KEEP | past findings to history |
| 50 | datadog: doubt the number (three cross-checks) | KEEP | |
| 51 | datadog funnel: signals_pull, compare, file at most three, SIGNALS.md | KEEP | |
| 52 | File through `issue_cluster.py file`; `gh issue create` is denied | CODE | nerd.fleet.json deny list; said in two lanes, now once for all |
| 53 | datadog: failed reads go on the `Broken:` line | KEEP | |
| 54 | devops: smoke-test, 1.5s, failures by class, incident not finding | KEEP | product baseline numbers to history |
| 55 | prod-runtime: why the lane exists | HISTORY | H§5 |
| 56 | prod-runtime: thresholds change only in a PR | KEEP | |
| 57 | prod-runtime 1: run `prod_runtime.py --file`; do not push again | KEEP | list of checks to H§5 (the script runs them) |
| 58 | prod-runtime 2-4: no access is the finding; explore; standard fixes | KEEP | |
| 59 | lens: what to check; label vs query; browser | KEEP | |
| 60 | revenue: precursor ordering; checks; never add billing code | KEEP | |
| 61 | revenue on fleet-kit: N/A with marker | KEEP | |
| 62 | Never build the fix | KEEP | |
| 63 | Report contents; lead with the biggest user-value finding | KEEP | |

Counts: KEEP 54, MERGE 5, CODE 1, SUPERSEDED 0, DEAD 1, HISTORY 2 (63 rows).

## Contradictions found

1. **Access.** The missing-credential example named `GOOGLE_CLIENT_ID`, while the table
   above it and the paragraph before it say the Google credentials are service-account key
   paths, "not OAuth client ids". The example is gone; the rule stays.
2. **Filing.** Only the datadog and prod-runtime lanes said to file through
   `issue_cluster.py` because `gh issue create` is denied. The deny is on the member, so it
   binds every lane; the other six lanes were never told how to file. Now said once, for all.
3. **Not fixed, flagged:** "check `gh pr list --search "<n> in:body"`" stays in nerd.md,
   while gru.md step 7 and `charter_bloat_check.py` both say that search returns mostly noise
   for short issue numbers. Kept as is (no behaviour change); see "least sure" in the PR.
4. **Not fixed, flagged:** the datadog funnel says "file at most three" under an UNCAPPED
   law. Read as scoped to funnel findings and kept, now worded that way.
5. **Not fixed (other file):** `nerd.fleet.json`'s checklist has a prod-readiness duty
   (`docs/prod_readiness.json`, owner=nerd) that the charter never mentions.
