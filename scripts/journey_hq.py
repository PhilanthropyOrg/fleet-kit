"""journey_hq -- the HQ feed journeys: every interactive thing a signed-in person does on
/network/hq, walked as the product's QA personas (owner / operator / visitor / verified).

Why a separate module: sentry's walk covered page LOADS, so on 2026-09-24 it missed that
every HQ reply 500'd on prod (fixed by philanthropy#7726) and that the React picker closes
when the pointer leaves the button on its way to the emoji. Both are ACTIONS, not loads.
Each journey here does the action, asserts what a person would see, reloads, and asserts
it stuck.

Test data: personas act only on their own fixture org's feed card (reserved 00-prefixed
EINs, is_fixture orgs, shown in the feed to is_qa viewers only), and POST /990/api/qa/reset
wipes each persona's comments/reactions/follows/claims at the start and end of a walk.

Registered into journey_walker.JOURNEY_RUNNERS via make_runners(jw), where jw is the walker
module itself -- passed in rather than imported so `python3 journey_walker.py` (which runs
as __main__) never loads a second copy whose Blocked class the runner loop would not catch.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

HQ = "https://philanthropy.org/network/hq"


def make_runners(jw) -> dict:
    Blocked = jw.Blocked

    def qa_post(users, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            users.url(f"https://philanthropy.org{path}"),
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-QA-Token": users.qa_session_token or "",
                     "User-Agent": "Mozilla/5.0 (fleet-kit sentry)",
                     **({jw.BYPASS_HEADER: users.bypass} if users.bypass else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 404):
                raise Blocked(f"QA endpoint {path} answered {exc.code}: QA_SESSION_TOKEN missing or endpoint not deployed") from exc
            raise

    def persona(ctx, name: str, next_path: str = "/network/hq"):
        """Reset the persona, sign it in, land on next_path. Returns (page, session info)."""
        users = ctx.users
        users.require_users(name)
        if not users.qa_session_token:
            raise Blocked("persona journeys need QA_SESSION_TOKEN")
        qa_post(users, "/990/api/qa/reset", {"user": name})
        ctx.cleanup.append(lambda: qa_post(users, "/990/api/qa/reset", {"user": name}))
        info = qa_post(users, "/990/api/qa/session", {"user": name, "next": next_path})
        page = ctx.page(name)
        page.goto(users.url(info["url"]), timeout=30000)
        jw.wait_path_no_longer_contains(page, "/auth/magic", timeout=15000)
        return page, info

    def card(page, ein: str):
        return page.locator(f"[data-event-id]:has(.hqf-react[data-ein='{ein}'])").first

    def open_hq_card(ctx, name: str):
        page, info = persona(ctx, name)
        ein = info.get("ein") or ctx.users.persona_eins.get(name)
        if not ein:
            raise Blocked(f"no fixture EIN known for persona {name}")
        page.goto(ctx.users.url(HQ), timeout=30000)
        c = card(page, ein)
        c.wait_for(state="visible", timeout=15000)
        return page, c, ein

    def slow_move(page, from_loc, to_loc, steps=30):
        a, b = from_loc.bounding_box(), to_loc.bounding_box()
        page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
        page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=steps)

    def react_label(c) -> str:
        return c.locator(".hqf-react").first.get_attribute("aria-label") or ""

    # --- React: hover, move slowly, pick, switch, remove, reload -----------------------------

    def run_hq_react(ctx):
        state = {}

        def s0():
            state["page"], state["card"], state["ein"] = open_hq_card(ctx, "owner")
        if not ctx.step(0, s0):
            return
        page, c = state["page"], state["card"]
        btn = c.locator(".hqf-react").first
        picker = c.locator(".hqf-picker[role='menu']").first

        def s1():
            btn.hover()
            picker.wait_for(state="visible", timeout=3000)
            state["emojis"] = [e.get_attribute("data-emoji") for e in picker.locator(".hqf-picker-emoji").all()]
            assert len(state["emojis"]) >= 2, f"picker shows {len(state['emojis'])} emoji"
        if not ctx.step(1, s1, page):
            return

        def pick(emoji):
            target = picker.locator(f".hqf-picker-emoji[data-emoji='{emoji}']").first
            if not picker.is_visible():
                btn.hover()
                picker.wait_for(state="visible", timeout=3000)
            slow_move(page, btn, target)
            page.wait_for_timeout(300)
            assert picker.is_visible(), "picker closed while the pointer moved from React to the emoji"
            box = target.bounding_box()
            on_top = page.evaluate(
                "([x, y, el]) => { const hit = document.elementFromPoint(x, y);"
                " return hit && (hit === el || el.contains(hit)) ? '' : (hit ? hit.outerHTML.slice(0, 120) : 'nothing'); }",
                [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, target.element_handle()])
            assert not on_top, f"emoji {emoji} is covered -- a person's pointer lands on {on_top!r}, not the picker"
            with page.expect_response(lambda r: "/network/hq/api/react/" in r.url and r.request.method == "POST") as resp:
                target.click()
            assert resp.value.status == 200, f"react POST returned {resp.value.status}"

        first, second = (lambda: state["emojis"][0]), (lambda: state["emojis"][1])

        def s2():
            pick(first())
        if not ctx.step(2, s2, page):
            return

        def s3():
            page.wait_for_function("([el, e]) => (el.getAttribute('aria-label')||'').includes(e)",
                                   arg=[btn.element_handle(), first()], timeout=5000)
        if not ctx.step(3, s3, page):
            return

        def s4():
            pick(second())
            page.wait_for_function("([el, e]) => (el.getAttribute('aria-label')||'').includes(e)",
                                   arg=[btn.element_handle(), second()], timeout=5000)
        if not ctx.step(4, s4, page):
            return

        def s5():
            page.reload(timeout=30000)
            c2 = card(page, state["ein"])
            c2.wait_for(state="visible", timeout=15000)
            label = react_label(c2)
            assert second() in label, f"after reload the React button reads {label!r}, expected {second()}"
        if not ctx.step(5, s5, page):
            return

        def s6():
            nonlocal c, btn, picker
            c = card(page, state["ein"])
            btn, picker = c.locator(".hqf-react").first, c.locator(".hqf-picker[role='menu']").first
            pick(second())  # same emoji again removes it
            page.reload(timeout=30000)
            c2 = card(page, state["ein"])
            c2.wait_for(state="visible", timeout=15000)
            label = react_label(c2)
            assert "Reacted" not in label, f"after remove + reload the button still reads {label!r}"
        ctx.step(6, s6, page)

    # --- Reply: post, see it, reload, still there ----------------------------------------------

    def run_hq_reply(ctx):
        state = {}
        marker = f"sentry check {ctx.run_id} {int(time.time())}"

        def s0():
            state["page"], state["card"], state["ein"] = open_hq_card(ctx, "owner")
            state["card"].locator(".hqf-reply").first.click()
            state["card"].locator(".hqf-comment-input").first.wait_for(state="visible", timeout=5000)
        if not ctx.step(0, s0):
            return
        page, c = state["page"], state["card"]

        def s1():
            c.locator(".hqf-comment-input").first.fill(marker)
            with page.expect_response(lambda r: "/network/hq/api/comments/" in r.url and r.request.method == "POST") as resp:
                c.locator(".hqf-comment-form").first.get_by_role("button", name=re.compile("post", re.I)).click()
            assert resp.value.status == 200, f"reply POST returned {resp.value.status}: {resp.value.text()[:200]}"
            c.get_by_text(marker).first.wait_for(state="visible", timeout=5000)
        if not ctx.step(1, s1, page):
            return

        def s2():
            page.reload(timeout=30000)
            c2 = card(page, state["ein"])
            c2.wait_for(state="visible", timeout=15000)
            c2.locator(".hqf-reply").first.click()
            c2.get_by_text(marker).first.wait_for(state="visible", timeout=8000)
        ctx.step(2, s2, page)

    # --- Share / Save / Follow / links ---------------------------------------------------------

    def run_hq_share_save_follow(ctx):
        state = {}

        def s0():
            state["page"], state["card"], state["ein"] = open_hq_card(ctx, "owner")
        if not ctx.step(0, s0):
            return
        page, c, ein = state["page"], state["card"], state["ein"]

        def s1():  # Share
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            page.evaluate("() => { if (navigator.share) navigator.share = d => { window.__sentryShared = d; return Promise.resolve(); }; }")
            share = c.locator(".hqf-share").first
            href = share.get_attribute("data-share-href") or ""
            share.click()
            page.wait_for_function(
                "([el]) => !!window.__sentryShared || /copied/i.test(el.innerText)",
                arg=[share.element_handle()], timeout=4000)
            got = page.evaluate("() => (window.__sentryShared && window.__sentryShared.url) || navigator.clipboard.readText()")
            assert href and href.split("#")[0] in (got or ""), f"shared {got!r}, expected the org link {href!r}"
        ctx.step(1, s1, page)

        def s2():  # Save
            save = c.locator("[data-track='hqf_save']").first
            before = save.inner_text().strip()
            save.click()
            page.wait_for_timeout(1500)
            after = save.inner_text().strip()
            pressed = save.get_attribute("aria-pressed")
            assert after != before or pressed == "true", f"Save did nothing: still reads {after!r}, no request, no state"
            page.reload(timeout=30000)
            s = card(page, ein).locator("[data-track='hqf_save']").first
            assert s.inner_text().strip() != before or s.get_attribute("aria-pressed") == "true", "Save did not survive reload"
        ctx.step(2, s2, page)

        def s3():  # Follow, reload, unfollow, reload
            def follow_btn():
                return card(page, ein).locator(".hqf-follow").first
            page.goto(ctx.users.url(HQ), timeout=30000)
            follow_btn().wait_for(state="visible", timeout=15000)
            if re.search("following", follow_btn().inner_text(), re.I):
                follow_btn().click()
                page.wait_for_timeout(1000)
            follow_btn().click()
            page.wait_for_function("([el]) => /following/i.test(el.innerText)", arg=[follow_btn().element_handle()], timeout=5000)
            page.reload(timeout=30000)
            try:  # follow.js hydrates the button from GET /990/api/me/follows after load
                page.wait_for_function("([el]) => /following/i.test(el.innerText)", arg=[follow_btn().element_handle()], timeout=8000)
            except Exception:
                raise AssertionError(f"Follow did not survive reload: reads {follow_btn().inner_text()!r}")
            follow_btn().click()
            page.wait_for_function("([el]) => /\\+?\\s*follow$/i.test(el.innerText.trim())", arg=[follow_btn().element_handle()], timeout=5000)
            page.reload(timeout=30000)
            page.wait_for_timeout(3000)  # let follow.js hydrate before judging
            assert not re.search("following", follow_btn().inner_text(), re.I), "Unfollow did not survive reload"
        ctx.step(3, s3, page)

        def s4():  # logo plate and org name both land on the org's page
            for sel in (".hqf-item-plate", ".hqf-item-org-link"):
                page.goto(ctx.users.url(HQ), timeout=30000)
                link = card(page, ein).locator(sel).first
                link.wait_for(state="visible", timeout=15000)
                name = card(page, ein).locator(".hqf-item-org-link").first.inner_text().strip()
                with page.expect_navigation(timeout=20000) as nav:
                    link.click()
                resp = nav.value
                assert resp is not None and resp.status < 400, f"{sel} -> {page.url} answered {resp.status if resp else 'nothing'}"
                assert name and name.lower() in page.inner_text("body").lower(), f"{sel} landed on {page.url} without the org name {name!r}"
        ctx.step(4, s4, page)

    # --- Claim to verified, with the live feed watching ------------------------------------------

    def run_hq_claim_to_verified_live(ctx):
        users, state = ctx.users, {}

        def s0():  # owner has HQ open (the live-feed watcher); visitor opens the claim form
            state["owner"], _ = persona(ctx, "owner")
            state["owner"].goto(users.url(HQ), timeout=30000)
            state["owner"].locator("#hqf-feed-list").first.wait_for(state="visible", timeout=15000)
            state["visitor"], info = persona(ctx, "visitor", next_path="/990")
            state["ein"] = info.get("ein") or users.persona_eins.get("visitor")
            assert state["ein"], "no fixture EIN for visitor"
            state["visitor"].goto(users.url(f"https://philanthropy.org/990/claim/{state['ein']}"), timeout=30000)
            state["visitor"].locator("#claim-form").first.wait_for(state="visible", timeout=15000)
        if not ctx.step(0, s0):
            return
        v, o = state["visitor"], state["owner"]

        def s1():  # visitor submits the claim
            v.locator("#claim-form [name='certify_authorized']").first.check()
            with v.expect_response(lambda r: f"/990/claim/{state['ein']}" in r.url and r.request.method == "POST") as resp:
                v.locator("#claim-form [type='submit']").first.click()
            assert resp.value.status < 400, f"claim POST returned {resp.value.status}"
            jw.wait_text_matches(v, r"pending|review|received|verified", timeout=15000)
        if not ctx.step(1, s1, v):
            return

        def s2():  # operator approves it through the review queue's own endpoint
            # Fixture claims are kept out of the human review queue, so the operator's mint
            # reports pending QA claims. (Re-minting the visitor would wipe its claim.)
            op, info = persona(ctx, "operator", next_path="/990/superadmin/verify")
            claim_id = next((c["claim_id"] for c in info.get("qa_pending_claims") or []
                             if c.get("ein") == state["ein"]), None)
            assert claim_id, f"no pending claim on {state['ein']} after the visitor submitted"
            resp = op.request.post(users.url("https://philanthropy.org/990/api/superadmin/verify/org-claim"),
                                   data={"id": claim_id, "action": "verify", "note": "test: sentry claim walk"},
                                   headers={"Origin": users.base_url.rstrip("/")})
            assert resp.status == 200, f"operator approve returned {resp.status}: {resp.text()[:200]}"
        if not ctx.step(2, s2, v):
            return

        def s3():  # the owner's open HQ shows the new claim live, no reload
            o.locator(f"[data-event-id]:has([data-ein='{state['ein']}'])").first.wait_for(state="visible", timeout=30000)
        ctx.step(3, s3, o)

        def s4():  # the visitor now owns it
            v.goto(users.url(f"https://philanthropy.org/990/claim/{state['ein']}"), timeout=30000)
            jw.wait_text_matches(v, r"verified|you manage|owner|admin", timeout=15000)
        ctx.step(4, s4, v)

    # --- every persona signs in and sees its role --------------------------------------------------

    def run_persona_roles(ctx):
        users = ctx.users
        checks = [
            ("owner", "/network/hq", "#hqf-feed-list"),
            ("verified", "/network/hq", "#hqf-feed-list"),
            ("operator", "/990", None),
            ("visitor", "/network/hq", "#hqf-feed-list"),
        ]
        for i, (name, path, selector) in enumerate(checks):
            def s(name=name, path=path, selector=selector):
                page, _ = persona(ctx, name, next_path=path)
                if selector:
                    resp = page.goto(users.url(f"https://philanthropy.org{path}"), timeout=30000)
                    assert resp is not None and resp.status < 400, f"{name} {path} answered {resp.status if resp else 'nothing'}"
                if selector:
                    page.locator(selector).first.wait_for(state="attached", timeout=15000)
                else:  # operator: the review queue's own decide endpoint accepts them as staff
                    # (a nonexistent claim id -> 404 "No such claim"; a non-admin gets 401/403)
                    r = page.request.post(users.url("https://philanthropy.org/990/api/superadmin/verify/org-claim"),
                                          data={"id": 0, "action": "verify"},
                                          headers={"Origin": users.base_url.rstrip("/")})
                    assert r.status == 404 and "claim" in r.text().lower(), f"operator is not staff: {r.status} {r.text()[:120]}"
            ctx.step(i, s, None)

    return {
        "persona-roles": run_persona_roles,
        "hq-react-picker": run_hq_react,
        "hq-reply-post-and-reload": run_hq_reply,
        "hq-share-save-follow-links": run_hq_share_save_follow,
        "hq-claim-to-verified-live-feed": run_hq_claim_to_verified_live,
    }
