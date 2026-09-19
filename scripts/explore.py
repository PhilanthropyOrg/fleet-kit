#!/usr/bin/env python3
"""explore.py -- a browser an LLM drives itself, one command at a time.

Reif, 2026-09-19: "it's fully scripted right? I want to give it an LLM and a job to do --
and see what it does. Ie like a real user."

journeys.yaml is a fixed catalogue: a person wrote the steps, so the walker can only find
breaks on paths someone already imagined. A real user has a GOAL and improvises. This gives
a member that: a persistent Chromium session and eight verbs, one subprocess call each, so
the model decides the next click from what it just saw.

    python3 scripts/explore.py open https://philanthropy.org/990
    python3 scripts/explore.py look                 # title, url, headings, visible text
    python3 scripts/explore.py links                # clickable things, numbered
    python3 scripts/explore.py click 7              # by number from `links`
    python3 scripts/explore.py click "Claim this org"   # or by text
    python3 scripts/explore.py type "#q" "red cross"
    python3 scripts/explore.py press Enter
    python3 scripts/explore.py back
    python3 scripts/explore.py shot                 # png path, for the report
    python3 scripts/explore.py close

State lives in --session-dir (default $EXPLORE_DIR or qa-out/explore/<name>): a
Playwright launch_persistent_context, so cookies and sign-in survive between calls.
Every call prints the page's console errors and any failed request since the last call --
that is the finding, most of the time.

Exit code is 0 for "the command ran" even when the page is broken; a broken page is data,
not a crash. Exit 2 means the command itself could not run (no session, bad selector).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
MAX_TEXT = 4000
MAX_LINKS = 60


def _session_dir(name: str) -> Path:
    base = os.environ.get("EXPLORE_DIR")
    p = Path(base) if base else Path("qa-out") / "explore" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ctx(sess: Path, viewport: str):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    w, h = VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
    headers = {}
    bypass = os.environ.get("ATLAS_TEST_BYPASS")
    if bypass:
        headers["X-Atlas-Test-Bypass"] = bypass
        headers["x-atlas-test"] = bypass
    ctx = pw.chromium.launch_persistent_context(
        str(sess / "profile"), headless=True, viewport={"width": w, "height": h},
        extra_http_headers=headers or None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text[:200]}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:200]}"))
    page.on("requestfailed", lambda r: errors.append(f"requestfailed {r.method} {r.url[:120]}: {r.failure}"))
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url[:120]}") if r.status >= 500 else None)
    return pw, ctx, page, errors


def _report(page, errors, extra: str = "") -> int:
    out = {"url": page.url, "title": page.title()}
    if extra:
        print(extra)
    print(f"\nurl: {out['url']}\ntitle: {out['title']}")
    if errors:
        print("\nPROBLEMS SEEN (this is what you are here to find):")
        for e in errors[:15]:
            print("  " + e)
    return 0


def _visible_text(page) -> str:
    txt = page.evaluate(
        "() => { const w=document.body?document.body.innerText:''; return w; }") or ""
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    joined = "\n".join(lines)
    return joined[:MAX_TEXT] + ("\n…(truncated)" if len(joined) > MAX_TEXT else "")


def _links(page) -> list[dict]:
    return page.evaluate(
        """() => [...document.querySelectorAll('a[href],button,[role=button],input[type=submit]')]
              .filter(e => { const r = e.getBoundingClientRect();
                             return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; })
              .slice(0, 200)
              .map((e,i) => ({ n: i, text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0,70),
                               tag: e.tagName.toLowerCase(), href: e.getAttribute('href') || '' }))
              .filter(o => o.text || o.href)""")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["open", "look", "links", "click", "type", "press", "back", "shot", "close"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--name", default=os.environ.get("EXPLORE_SESSION", "default"))
    ap.add_argument("--viewport", default="desktop", choices=sorted(VIEWPORTS))
    ap.add_argument("--wait", type=float, default=2.0, help="seconds to settle after an action")
    a = ap.parse_args(argv)

    sess = _session_dir(a.name)
    if a.command == "close":
        prof = sess / "profile"
        lock = prof / "SingletonLock"
        if lock.exists():
            lock.unlink()
        print(f"session {a.name} released ({prof})")
        return 0

    pw, ctx, page, errors = _ctx(sess, a.viewport)
    try:
        last = sess / "last_url"
        if a.command != "open" and not page.url.startswith("http") and last.exists():
            page.goto(last.read_text().strip(), wait_until="domcontentloaded", timeout=45000)
            errors.clear()

        if a.command == "open":
            if not a.args:
                print("usage: explore.py open <url>", file=sys.stderr)
                return 2
            page.goto(a.args[0], wait_until="domcontentloaded", timeout=60000)
        elif a.command == "click":
            if not a.args:
                print("usage: explore.py click <number|text>", file=sys.stderr)
                return 2
            target = " ".join(a.args)
            if target.isdigit():
                items = _links(page)
                match = next((o for o in items if o["n"] == int(target)), None)
                if not match:
                    print(f"no clickable #{target}; run `links` again", file=sys.stderr)
                    return 2
                page.get_by_text(match["text"], exact=False).first.click(timeout=15000) if match["text"] \
                    else page.click(f"a[href='{match['href']}']", timeout=15000)
            else:
                page.get_by_text(target, exact=False).first.click(timeout=15000)
        elif a.command == "type":
            if len(a.args) < 2:
                print("usage: explore.py type <selector> <text>", file=sys.stderr)
                return 2
            page.fill(a.args[0], " ".join(a.args[1:]), timeout=15000)
        elif a.command == "press":
            page.keyboard.press(a.args[0] if a.args else "Enter")
        elif a.command == "back":
            page.go_back(wait_until="domcontentloaded", timeout=45000)

        time.sleep(a.wait)
        (sess / "last_url").write_text(page.url)

        if a.command == "links":
            items = _links(page)[:MAX_LINKS]
            body = "\n".join(f"  [{o['n']}] {o['tag']}: {o['text']!r} {o['href']}" for o in items)
            return _report(page, errors, f"clickable ({len(items)} shown):\n{body}")
        if a.command == "shot":
            shot = sess / f"shot-{int(time.time())}.png"
            page.screenshot(path=str(shot), full_page=True)
            return _report(page, errors, f"screenshot: {shot}")
        return _report(page, errors, _visible_text(page) if a.command in ("open", "look", "click", "type", "press", "back") else "")
    except Exception as exc:  # noqa: BLE001
        print(f"COMMAND FAILED: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        if errors:
            print("problems seen before the failure:", file=sys.stderr)
            for e in errors[:10]:
                print("  " + e, file=sys.stderr)
        return 2
    finally:
        try:
            ctx.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
