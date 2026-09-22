# sentry's UX-judgment pass: the workflow lens + what good looks like

Companion to `docs/quality-standard.md` (the build-time bar) and `members/vp/vp.md` (the
acceptance judge). Those gate a specific item before/after it ships. This doc is for
**sentry's step 9** — the unscripted explore pass that runs every 3 hours with no item
attached, on whatever surface sentry chose. It needs its own judgment because it has no PRD,
no reference matrix, no VP waiting to review it: sentry is the only reader of what it saw.

## The workflow lens (read this before judging anything)

Every page exists because a person is mid-task. Before judging pixels, name the task in one
sentence, same shape as a PRD's "the person, and what they're trying to do"
(`docs/quality-standard.md` §0):

- *A funder on `/990/report/<ein>` is deciding whether to write a check.* They need the
  financials legible in under 10 seconds, not a design opinion.
- *A nonprofit staffer on `/990/claim/<ein>` is proving they run this org.* They need to know
  what step they're on and what happens if they stop.
- *A donor on `/990/?q=<name>` is checking a charity is real before giving.* They need one
  clearly-right result, not five ambiguous ones.

**Judge against the task, not against taste.** "This spacing feels off" is not a finding.
"The claim button is below the fold on a 14-day-old fixed-width phone screenshot, so the
person doing the actual task never sees it" is. If you can't state whose task is blocked and
how, it isn't a finding yet — keep looking or drop it.

## What good looks like, concretely

Borrow `docs/quality-standard.md`'s reference-product habit instead of reinventing criteria:

| this surface's job | look at how these do it |
|---|---|
| a form with real stakes (claim, sign in) | Stripe checkout, Linear onboarding — one question per screen, always-visible progress, a next step that's never ambiguous |
| a data-dense record page (990 report) | Crunchbase company page, SEC EDGAR filing viewer — the number the visitor came for is above the fold, not buried under navigation |
| search results | Google, Algolia-powered search UIs — the top result answers the query without a click; an empty state says why, not just "no results" |
| a feed / discovery surface | LinkedIn, Twitter/X — infinite scroll never dead-ends, every card is scannable in under 2 seconds |

You don't need a captured screenshot library for this pass (that's the `world-class`-label
research pass's job, done once per built item). You need the *habit*: before calling
something fine, ask "would Stripe/Linear/EDGAR ship this exact state to a real user?" If the
answer is "no, and here's the specific thing they'd fix," that's your finding.

## The five questions, in order

1. **What is the person here to do?** One sentence. If you can't write it, you're not looking
   at a real task — pick a different surface.
2. **Did they get it in the time they'd tolerate?** Above-the-fold for the thing they came
   for; under ~10 seconds to the answer; no scroll-and-hunt for a primary action.
3. **Is the next step ever ambiguous?** A form with no visible progress, a button with no
   visible result, a redirect with no explanation — each is a place a real person stops.
4. **Would a reference product (see table) ship this state?** Not "is this pretty" — "is this
   the quality bar a person expects from software today."
5. **Would it embarrass us?** `vp.md`'s own last question. If a journalist screenshotted this
   exact state tomorrow, is there anything you'd want pulled first?

## Filing it

Same mechanism as any other sentry finding (`sentry.md` step 8/9): one issue, titled with the
surface and the specific problem a person hits, screenshot attached, labeled
`fleet:backlog,lane:ui`. Not `lane:quality` — a UX finding is a design/build ask for the `ui`
lane (or `vp`/`marie` to rank), not a QA-mechanical break for sentry's own lane. Say which of
the five questions it failed and why, in one line — that's the evidence, not a taste claim.

A surface that passes all five is also worth one line in the report, same as a completed
explore goal: it's the only evidence anyone has that this specific job is good, not just
working.
