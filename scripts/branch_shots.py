#!/usr/bin/env python3
"""branch_shots.py -- styled 1440 + 390 screenshots of a page from THIS checkout's code.

PR #14357 (the OrgVerify top item, #12943) stayed a draft for 3 minion passes, 10-25..27:
judge-judy blocked every head for a design walk with "layout unjudged, stylesheets were not
loaded". A changed page is not on prod yet, a signed-in one needs a session, and HTML shot
on its own loses its /990/static CSS. This saves the page and every /990/static file it links
(fetched through the app's own test client, so the branch's CSS), then shoots the saved copy.

The app needs the test venv; Playwright needs system python3. So, two halves:

    PY=$(bash /fleet-kit/scripts/test_python.sh)
    PYTHONPATH=src $PY /fleet-kit/scripts/branch_shots.py dump --path /990/login --out /tmp/s
    PYTHONPATH=src $PY /fleet-kit/scripts/branch_shots.py dump --html page.html --out /tmp/s
    python3 /fleet-kit/scripts/branch_shots.py shoot --out /tmp/s --name login --dest DIR

`--html` is for a signed-in page: write `client.get(...).text` from your own test (its fixture
seeds the claim and the session) to a file. `shoot` prints the paths for `Design walk:`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

STATIC_RE = re.compile(r'="(/990/static/[^"?#]+)')
WIDTHS = ((1440, 900), (390, 844))


def localize(html: str) -> str:
    """Point /990/static links at the saved copies beside page.html."""
    return html.replace('="/990/static/', '="990/static/')


def dump(path: str | None, html_file: str | None, out: str) -> None:
    from fastapi.testclient import TestClient

    from philanthropy.api.app import app

    client = TestClient(app)
    html = open(html_file).read() if html_file else client.get(path).text
    for url in sorted(set(STATIC_RE.findall(html))):
        r = client.get(url)
        target = out + url
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(r.content)
        if r.status_code != 200:
            print(f"warn: {r.status_code} {url}", file=sys.stderr)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "page.html"), "w") as f:
        f.write(localize(html))
    print(os.path.join(out, "page.html"))


def shoot(out: str, name: str, dest: str) -> None:
    from playwright.sync_api import sync_playwright

    os.makedirs(dest, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        for w, h in WIDTHS:
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.goto(f"file://{os.path.abspath(out)}/page.html")
            shot = os.path.join(dest, f"{name}-{w}.png")
            pg.screenshot(path=shot, full_page=True)
            print(f"{w} {shot}")
        b.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--path")
    src.add_argument("--html")
    d.add_argument("--out", required=True)
    s = sub.add_parser("shoot")
    s.add_argument("--out", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--dest", help="where the PNGs go (default: --out)")
    a = ap.parse_args(argv)
    if a.cmd == "dump":
        dump(a.path, a.html, a.out)
    else:
        shoot(a.out, a.name, a.dest or a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
