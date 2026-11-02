# Value rubric

One rubric, two readers: judge-judy's value gate puts the two sections below into its prompt as they stand
(judge-judy.sh), and G scores every Owner decision with this file (Reif 11-02). Change it here only; never copy it.
Each risk is scored 0-3 (0 none, 1 weak, 2 solid, 3 best in class) with one line of why.

## The four risks
- value: will people want it? Utility (a real person gets what they came for, or saves a step), delight (Kano: basic, performance or delighter), and cuts (code, steps or pages removed with no loss of value earn points; a pure refactor that cuts code scores here).
- usability: can people use it? Nielsen's 10 usability heuristics and the design rules below, at 390 and 1440 wide. A change with nothing a person sees scores 2 unless it makes something slower or harder; 3 only for a visible, clear improvement.
- feasibility: can it be built and run well? Scales to every org (all 1.8M) and 10x traffic with no hand work per item; maintainable, fast, reliable, secure (ISO/IEC 25010). Your correctness findings land here.
- viability: does it work for the business? One of a thing (one HQ at /hq, one org record everywhere), hub and spoke, every page a place to give, and our advantages: public hard-to-read data made beautiful and useful, matching donors and providers to orgs, verified org owners, a festival of giving.

## Anchors
Anchors (real philanthropy PRs from 10-30 and 10-31; score against these, not against a default of 2):
- value 0: 14835 adds docs/ui-rules.md, rules for this reviewer, no end user. 14824 a cache-key test guard, no named user.
- value 1: 14785 renames the paid report on the Terms page, which few read, no number moves. 14907 one line on a report changes font.
- value 2: 14995 the paid foundation report stops reading a blank line as 0 gifts, so buyers get a revenue bridge that closes. 14988 Workday apply links stop answering 404 for job seekers.
- value 3: 14970 one recovery reminder when a verified checkout expires unpaid, buyers come back and pay (checkouts recovered). 14985 drops Microsoft Clarity from every page, 378 lines cut, every visitor loads less.
- value 2, red-main fix (Operations, PHIA-2214; Paul 11-02): a PR that only turns a red DEPLOY test gate green (a test, a registry entry or a revert, no page change) while main has not deployed since a green sha scores value 2 on the deploy it unblocks. The user is the visitor the held merged fixes serve (every visitor when the body names none), the number is the held merged PRs that ship once a deploy passes, and the baseline is the hours since the last green deploy and how many merged PRs wait behind it, from the body or the DEPLOY run it names. Check that the diff touches no page; a test that is only weakened to pass is a correctness finding, not a fix. Set beneficiary.tool true; never value 3. Examples: app 15155 registers a crontab cap and fixes a test-order footer test, releasing about 20 merged PRs held since 8e156927d; app 15206 points three Jobs tests at the header Post a job link, with no deploy since 07:37 UTC.
- value 2, measuring tools (G, PHIA-2043): a PR that changes no page but makes points, pay or the gate follow one visitor number already measured today (a kpi_daily metric such as jobs/applies, dash_metric_points, Search Console, events) scores value on that number: 2 when the PR names that one number and where it is read, as before when it names none. Set beneficiary.tool true for these; a tool PR is never value 3. Correctness, feasibility and research are judged in full. Example: box 247 pays the second half of a PR's points only when jobs/applies moved after merge.
- value 2, Owner-asked cost (G, PHIA-2067 and PHIA-2070): a PR that cuts CI or platform cost the Owner asked for scores value on platform/ci_usd_per_day (GitHub billing usage, the Automation watch list) or platform/fix_pr_wait_hours (median hours from a fix PR opening to merge). Here the user is the Owner, who pays the bill and waits for fixes. It is 2 when the PR names one of those numbers and how much it should fall, and as before when it names neither. Set beneficiary.tool true; never value 3. Example: app 15042, which cuts CI minutes per merged PR.
- usability 0: a person can no longer reach or read what they came for (a page 500s, a button does nothing, a new top-level section on a designed report, rule 1).
- usability 1: it works but adds a second name for one thing or a new box where one line would do (rules 3 and 4).
- usability 2: nothing a person sees changes, or a visible change that follows the design rules.
- usability 3: 14896 hub pages on a phone show the list on the first screen, two lines a row. 14967 long hub rows wrap instead of being cut off.
- feasibility 0: a correctness defect you block on. 1: hand work per item or a fix that will not last (14940 bumps a page cache key by hand to make a price show). 2: works and is tested at today's size. 3: 14952 a cached report no longer waits behind cold builds, so it holds at 10x traffic.
- viability 0: 14431 a live cron that changes Stripe billing and mails price changes before the Owner approved it. 1: a second copy of a thing we already have one of. 2: neutral for the business. 3: 14989 Billing drops its own verify cards for the one verify card on Home, one of a thing, 192 lines cut.
A score of 2 is a claim, not a default: every why says what would make it a 3. A table of 2/2/2/2 that does not say what would make each a 3 is a wrong answer.
