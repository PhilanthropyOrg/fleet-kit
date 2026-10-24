"""journey_money -- the daily walk's journeys (philanthropy#13448): the three money journeys a
person takes (claim -> HQ -> verify, job seeker -> apply, donor -> give) and one random sample
of public pages, each walked READ-ONLY on prod.

Read-only means: no claim is filed, no application sent, no gift given, no checkout session
started. Claim and verify screens are opened and read. The Apply hop is read with one plain
request that does not follow the redirect (no cookie, no referer, a headless user agent, so
the product's funnel counts nothing). The Pledge form is checked to be on the page and drawn;
nothing is typed into it. Personas are minted (POST /990/api/qa/session) but never reset, so a
sentry pass walking the same personas is not disturbed.

Every step puts what it saw on its result: `url` (where the browser was) and `seen` (the words
on the page that decided the verdict). scripts/daily_walk.py turns those into the walk file.

Random picks are seeded (DAILY_WALK_SEED, default the Central date), so a re-run on the same
day walks the same pages and a FAIL can be looked at twice.

Registered into journey_walker.JOURNEY_RUNNERS via make_runners(jw), same as journey_hq.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import random
import re
import urllib.error
import urllib.request
import zoneinfo
from urllib.parse import urljoin, urlsplit

# A headless UA: the product's bot filter (platform/bots.py matches "headless") keeps these
# reads out of the jobs and claim funnels.
WALK_UA = "Mozilla/5.0 (fleet-kit daily-walk; headless)"
# 53-0196605 (American Red Cross) renders the Pledge give form on its 990 report (gh#13364).
GIVE_EIN = os.environ.get("DAILY_WALK_GIVE_EIN", "530196605")
CHALLENGE = re.compile(r"just a moment|verifying you are human|attention required|checking your browser", re.I)
_SECRET_QS = re.compile(r"((?:^|[?&])(?:token|t|code|key|sig)=)[^&#\s\"']+", re.I)


def seed() -> str:
    return os.environ.get("DAILY_WALK_SEED") or dt.datetime.now(zoneinfo.ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")


def rng(purpose: str) -> random.Random:
    """One stream per pick, so adding a pick never moves another."""
    return random.Random(f"{seed()}:{purpose}")


def scrub_url(url: str) -> str:
    """A URL safe to print: sign-in and claim tokens in the query are cut."""
    return _SECRET_QS.sub(lambda m: m.group(1) + "[cut]", url or "")


def words(text: str, n: int = 150) -> str:
    t = re.sub(r"\s+", " ", text or "").strip().replace('"', "'")
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "..."


def make_runners(jw) -> dict:
    StepBlocked = jw.StepBlocked

    def same_site(users, url: str) -> bool:
        host, base = urlsplit(url).netloc, urlsplit(users.base_url).netloc
        return host == base or host.endswith("." + base)

    def fetch(users, url: str, follow: bool = True, timeout: int = 60):
        """(status, headers, body bytes) for a plain GET, bypass header only to our own site."""
        headers = {"User-Agent": WALK_UA}
        if users.bypass and same_site(users, url):
            headers[jw.BYPASS_HEADER] = users.bypass

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(*([] if follow else [_NoRedirect]))
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read() or b""

    def sitemap_locs(users, url: str) -> list[str]:
        status, _, body = fetch(users, url)
        if status >= 400:
            raise AssertionError(f"{url} answered {status}")
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return re.findall(r"<loc>([^<]+)</loc>", body.decode("utf-8", "ignore"))

    def open_page(ctx, page, url: str):
        """Open url, record it, and turn a challenge page into BLOCKED (the checker's fault)
        and a 4xx/5xx into a failed step (the product's)."""
        url = ctx.users.url(url) if url.startswith(("/", "https://philanthropy.org")) else url
        ctx.evidence["url"] = scrub_url(url)
        resp = page.goto(url, timeout=jw.NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        ctx.evidence["url"] = scrub_url(page.url)
        status = resp.status if resp is not None else None
        title = page.title() or ""
        mitigated = resp is not None and "cf-mitigated" in {k.lower() for k in resp.headers}
        if CHALLENGE.search(title) or (status == 403 and mitigated):
            raise StepBlocked(f"Cloudflare challenge page ({status}) -- the checker was not let in")
        if status is not None and status >= 400:
            raise AssertionError(f"{scrub_url(page.url)} answered {status}")
        return resp

    def heading(page) -> str:
        h = page.locator("h1")
        return words(h.first.inner_text(timeout=8000), 90) if h.count() else ""

    def sign_in(ctx, name: str, next_path: str):
        """A persona's own page, signed in through the QA magic link (minted, never reset)."""
        users = ctx.users
        if not users.qa_session_token:
            raise StepBlocked("QA_SESSION_TOKEN missing: cannot sign in as a QA persona")
        page = ctx.page(name)
        try:
            link = jw.qa_session_url(users, {"name": name}, next_path)
        except jw.Blocked as exc:
            raise StepBlocked(str(exc)) from exc
        page.goto(users.url(link), timeout=jw.NAV_TIMEOUT_MS)
        jw.wait_path_no_longer_contains(page, "/auth/magic", timeout=15000)
        ctx.evidence["url"] = scrub_url(page.url)
        if "/login" in urlsplit(page.url).path:
            raise AssertionError(f"the {name} sign-in link bounced to {scrub_url(page.url)}")
        return page

    def first_visible(locator):
        for i in range(min(locator.count(), 12)):
            if locator.nth(i).is_visible():
                return locator.nth(i)
        return None

    # --- 1. claim -> HQ -> verify ---------------------------------------------------------------

    def run_claim_hq_verify(ctx):
        users, st = ctx.users, {}
        ein = users.fixture_ein or GIVE_EIN
        anon = ctx.page()

        def s0():
            open_page(ctx, anon, f"/990/report/{ein}")
            link = first_visible(anon.locator("a[href*='/claim/']", has_text=re.compile("claim", re.I)))
            assert link is not None, "no visible Claim link on the report"
            st["claim_href"] = urljoin(anon.url, link.get_attribute("href"))
            ctx.evidence["seen"] = f"{heading(anon)} · {words(link.inner_text(), 40)} -> {urlsplit(st['claim_href']).path}"

        def s1():
            open_page(ctx, anon, st["claim_href"])
            body = anon.locator("body").inner_text()
            assert re.search(r"claim", body, re.I), "the claim screen never says claim"
            form = anon.locator("#claim-form").count() or jw.email_field(anon).count()
            assert form, "no claim form and no sign-in field on the claim screen"
            ctx.evidence["seen"] = words(heading(anon) or body, 140)
        if ctx.step(0, s0, anon):  # the owner half below runs either way: it needs no Claim link
            ctx.step(1, s1, anon)

        def s2():
            page = sign_in(ctx, "owner", "/hq")
            st["owner"] = page
            path = urlsplit(page.url).path.rstrip("/")
            hq = page.locator("a", has_text=re.compile(r"^\s*HQ\s*$")).first
            hq_href = hq.get_attribute("href") if hq.count() else None
            ctx.evidence["seen"] = f"landed on {path or '/'}: {words(page.title(), 70)}; masthead HQ link -> {hq_href or 'none'}"
            assert path == "/hq", f"the owner's door to HQ landed on {path}, not /hq"
        if not ctx.step(2, s2, ctx.page("owner")):
            return
        owner = st["owner"]

        def s3():
            # The desktop card (#get-verified-cta) sits in a side slot that is display:none at 390;
            # the phone gets its own copy, a plain <section> led by an h2[data-ask]. Take the one shown.
            card = owner.locator("[data-hq-verify-card]:visible, #get-verified-cta:visible, "
                                 "section:has(> h2[data-ask]):visible").filter(has_text=re.compile(r"verif", re.I)).first
            card.wait_for(state="visible", timeout=15000)
            text = card.inner_text()
            ctx.evidence["seen"] = words(text, 150)
            assert re.search(r"verif", text, re.I), "the HQ card does not say verify"
            assert re.search(r"\d+\s+of\s+\d+", text, re.I), "the checklist card shows no 'N of M' count"
            hrefs = card.locator("a[href]").evaluate_all("els => els.map(e => e.getAttribute('href'))")
            m = next((re.search(r"ein=(\d{9})", h) for h in hrefs if re.search(r"ein=(\d{9})", h or "")), None)
            st["ein"] = m.group(1) if m else None
            st["verify"] = next((h for h in hrefs if "/orgverify" in (h or "") and "badge-offer" not in h), None)
        if not ctx.step(3, s3, owner):
            return

        def s4():
            target = st.get("verify") or (f"/network/hq/orgverify?ein={st['ein']}" if st.get("ein") else "/network/hq/orgverify")
            open_page(ctx, owner, urljoin(owner.url, target))
            body = owner.locator("body").inner_text()
            m = re.search(r"(step \d+ of \d+[^\n]{0,80})", body, re.I)
            ctx.evidence["seen"] = words(m.group(1) if m else body[:150], 140)
            assert re.search(r"step \d+ of \d+|get (your org )?verified", body, re.I), "the verify screen shows no step"
            if not st.get("ein"):
                m2 = re.search(r"EIN (\d{2})-(\d{7})", body)
                st["ein"] = m2.group(1) + m2.group(2) if m2 else None
        if not ctx.step(4, s4, owner):
            return

        def s5():
            assert st.get("ein"), "no EIN for the owner's org on the card or the verify screen"
            url = users.url(f"/orgverify/api/{st['ein']}/pay")
            ctx.evidence["url"] = url
            resp = owner.request.get(url, headers={jw.BYPASS_HEADER: users.bypass} if users.bypass else {})
            assert resp.status == 200, f"the pay rule answered {resp.status}"
            v = resp.json()
            prices = v.get("prices") or []
            assert prices, "the pay rule lists no price"
            st["can_pay"] = bool(v.get("can_pay"))
            ctx.evidence["seen"] = f"can pay now: {'yes' if st['can_pay'] else 'no'}; {prices[0].get('range')}: {prices[0].get('price')}"
        if not ctx.step(5, s5, owner):
            return

        def s6():
            ctx.evidence["url"] = scrub_url(owner.url)
            if not st.get("can_pay"):
                raise StepBlocked("the owner persona has not passed proof, so Pay is not offered yet; no QA persona sits at proof-passed-not-paid")
            pay = first_visible(owner.locator("a, button", has_text=re.compile(r"pay and go live|pay", re.I)))
            ctx.evidence["seen"] = words(pay.inner_text(), 60) if pay is not None else "no Pay control"
            assert pay is not None, "proof passed but the verify screen shows no Pay control"
            raise StepBlocked("Pay is offered; the walk stops before the Stripe hop, which would start a real checkout session")
        ctx.step(6, s6, owner)

    # --- 2. job seeker -> apply -----------------------------------------------------------------

    def run_job_seeker_apply(ctx):
        users, st = ctx.users, {}
        page = ctx.page()

        def s0():
            open_page(ctx, page, "/jobs")
            hrefs = page.eval_on_selector_all("a[href*='/jobs/j/']", "els => els.map(e => e.getAttribute('href'))")
            hrefs = list(dict.fromkeys(h for h in hrefs if h and "/go" not in h and "/apply" not in h))
            assert hrefs, "/jobs lists no job"
            st["jobs"] = hrefs[:30]
            ctx.evidence["seen"] = f"{heading(page)} · {len(hrefs)} job links"
        if not ctx.step(0, s0, page):
            return

        def s1():
            href = rng("job-seeker-apply").choice(st["jobs"])
            open_page(ctx, page, urljoin(page.url, href))
            title = heading(page)
            assert title, "the job page has no title"
            ctx.evidence["seen"] = title
        if not ctx.step(1, s1, page):
            return

        def s2():
            apply = first_visible(page.get_by_role("link", name=re.compile(r"^\s*apply\s*$", re.I)))
            assert apply is not None, "no visible Apply control on the job page"
            st["apply"] = urljoin(page.url, apply.get_attribute("href"))
            ctx.evidence["seen"] = f"Apply -> {urlsplit(st['apply']).path}"
        if not ctx.step(2, s2, page):
            return

        def s3():
            status, headers, _ = fetch(users, st["apply"], follow=False)
            loc = urljoin(st["apply"], headers.get("Location") or headers.get("location") or "")
            ctx.evidence["url"] = scrub_url(st["apply"])
            if status in (301, 302, 303, 307, 308) and loc:
                if not same_site(users, loc):
                    ctx.evidence["seen"] = f"{status} to the employer's listing at {urlsplit(loc).netloc}"
                    return
                st["gate"] = loc
            elif status == 200:
                st["gate"] = st["apply"]
            else:
                raise AssertionError(f"Apply answered {status} with no place to go")
            open_page(ctx, page, st["gate"])
            body = page.locator("body").inner_text()
            ctx.evidence["seen"] = f"{status} to {urlsplit(st['gate']).path}: " + words(heading(page) or body, 110)
            assert re.search(r"sign in|join|apply|account", body, re.I), "the apply gate says nothing about applying or signing in"
        ctx.step(3, s3, page)

    # --- 3. donor -> give -----------------------------------------------------------------------

    def run_donor_give(ctx):
        users = ctx.users
        page = ctx.page()

        def s0():
            open_page(ctx, page, f"/990/report/{GIVE_EIN}")
            give = page.locator("summary.give-btn, #give, #m-give, [data-track='pledge_give_open']")
            assert give.count(), "no Give control on the charity page"
            ctx.evidence["seen"] = f"{heading(page)} · {words(give.first.inner_text(), 40) or 'Give'}"
        if not ctx.step(0, s0, page):
            return

        def s1():
            div = page.locator(".plg-donate[data-ein]")
            script = page.locator("script[src*='pledge.to/embed/widget.js']")
            assert div.count(), "no Pledge form (plg-donate) on the page"
            assert script.count(), "the Pledge widget script (pledge.to/embed/widget.js) is not on the page"
            ctx.evidence["seen"] = f"plg-donate data-ein={div.first.get_attribute('data-ein')}; script {urlsplit(script.first.get_attribute('src')).netloc}/embed/widget.js"
        if not ctx.step(1, s1, page):
            return

        def s2():
            if urlsplit(users.base_url).scheme != "https":
                raise StepBlocked("the Pledge form draws only on https; this walk is not on https")
            give = page.locator("summary.give-btn")
            if give.count() and give.first.is_visible():
                give.first.click()  # opens the <details>; nothing is typed or sent
            frame = None
            for _ in range(40):
                frame = next((f for f in page.frames if "pledge.to" in urlsplit(f.url).netloc), None)
                if frame:
                    break
                page.wait_for_timeout(500)
            assert frame, "the Pledge form iframe never drew within 20s"
            ctx.evidence["seen"] = f"Pledge iframe from {urlsplit(frame.url).netloc}{urlsplit(frame.url).path[:28]}"
        ctx.step(2, s2, page)

    # --- 4. random pages ------------------------------------------------------------------------

    def run_random_pages(ctx):
        users, st = ctx.users, {}
        page = ctx.page()

        def s0():  # one 990 report from a random shard of the 990 sitemap
            shards = [u for u in sitemap_locs(users, users.url("/990/sitemap.xml")) if re.search(r"/sitemap-\d+\.xml", u)]
            assert shards, "the 990 sitemap lists no report shards"
            shard = rng("report-shard").choice(shards)
            reports = [u for u in sitemap_locs(users, users.url(urlsplit(shard).path)) if "/990/report/" in u]
            assert reports, f"{urlsplit(shard).path} lists no report"
            open_page(ctx, page, users.url(urlsplit(rng("report").choice(reports)).path))
            jw.wait_text_matches(page, r"\$[0-9]|revenue|expense", timeout=10000)
            body = page.locator("body").inner_text()
            fig = re.search(r"\$[0-9][0-9,.]*\s*[KMB]?", body)
            st["people"] = page.eval_on_selector_all("a[href*='/990/person/']", "els => els.map(e => e.getAttribute('href'))")
            ctx.evidence["seen"] = f"{heading(page)} · {fig.group(0) if fig else 'a revenue/expense line'}"
            assert heading(page), "the report has no org name"
        ctx.step(0, s0, page)

        def s1():  # one public org page: an employer from the jobs sitemap, by its /990/org door
            locs = [u for u in sitemap_locs(users, users.url("/jobs/sitemap-jobs-wave-1.xml")) if "/jobs/at/" in u]
            eins = [m.group(1) for m in (re.search(r"/jobs/at/(\d{9})", u) for u in locs) if m]
            assert eins, "the jobs sitemap lists no employer"
            open_page(ctx, page, f"/990/org/{rng('org').choice(eins)}")
            name = heading(page)
            ctx.evidence["seen"] = f"{urlsplit(page.url).path.split('/')[1] or '/'} page: {name}"
            assert name, "the org page has no name"
        ctx.step(1, s1, page)

        def s2():  # one open job from the jobs sitemap
            jobs = [u for u in sitemap_locs(users, users.url("/jobs/sitemap-jobs-open.xml")) if "/jobs/j/" in u]
            assert jobs, "the open-jobs sitemap lists no job"
            open_page(ctx, page, users.url(urlsplit(rng("job").choice(jobs)).path))
            title = heading(page)
            apply = page.get_by_role("link", name=re.compile(r"^\s*apply\s*$", re.I)).count()
            ctx.evidence["seen"] = f"{title} · {'Apply shown' if apply else 'no Apply'}"
            assert title, "the job page has no title"
            assert apply, "an open job from the sitemap shows no Apply control"
        ctx.step(2, s2, page)

        def s3():  # one person page: an officer on the random report, else the people list
            people = [h for h in (st.get("people") or []) if h]
            if not people:
                open_page(ctx, page, "/990/people/")
                people = page.eval_on_selector_all("a[href*='/990/person/']", "els => els.map(e => e.getAttribute('href'))")
            assert people, "no person link on the report or the people list"
            open_page(ctx, page, urljoin(users.base_url, rng("person").choice(people)))
            name = heading(page)
            ctx.evidence["seen"] = f"{name} · {words(page.title(), 70)}"
            assert name, "the person page has no name"
        ctx.step(3, s3, page)

        def s4():  # one HQ screen as the owner persona
            owner = sign_in(ctx, "owner", "/hq")
            hrefs = owner.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
            screens = sorted({urlsplit(h).path for h in hrefs if h and re.match(r"^/(hq|network/hq)(/|$)", urlsplit(h).path)
                              and not re.search(r"/ops|sign-?out|logout|badge-offer", h)})
            assert screens, "the owner's HQ links to no HQ screen"
            open_page(ctx, owner, rng("hq").choice(screens))
            assert "/login" not in owner.url, f"the owner was sent to {scrub_url(owner.url)}"
            hd = owner.locator("h1, h2").first
            ctx.evidence["seen"] = f"{words(owner.title(), 70)} · {words(hd.inner_text(), 60) if hd.count() else 'no heading'}"
        ctx.step(4, s4, ctx.page("owner"))

        def s5():  # one automatic mail: the last kept copy of a random sequence, read as staff
            op = sign_in(ctx, "operator", "/network/hq/ops/mail")
            hrefs = op.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
            flows = sorted({h for h in hrefs if h and re.search(r"/mail/[a-z0-9_]+$", h) and not h.endswith("/bounces")})
            assert flows, "the staff Mail page lists no mail sequence"
            rng("mail").shuffle(flows)
            seen = ""
            for href in flows[:3]:
                open_page(ctx, op, urljoin(op.url, href))
                box = op.locator("section.opm-mail")
                text = box.first.inner_text() if box.count() else ""
                m = re.search(r"Subject\s*(.+)", text)
                label = words(op.locator("h2").first.inner_text(), 50) if op.locator("h2").count() else href
                seen = f"{label}: " + (f"last mail '{words(m.group(1), 80)}'" if m else "no mail kept yet")
                if m:
                    frame = op.locator("iframe.opm-frame")
                    assert frame.count(), "a kept mail shows no preview"
                    break
            ctx.evidence["seen"] = seen
        ctx.step(5, s5, ctx.page("operator"))

    return {
        "walk-claim-hq-verify": run_claim_hq_verify,
        "walk-job-seeker-apply": run_job_seeker_apply,
        "walk-donor-give": run_donor_give,
        "walk-random-pages": run_random_pages,
    }
