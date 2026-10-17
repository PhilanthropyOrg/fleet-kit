#!/usr/bin/env python3
"""audit_990_truth -- auditor lane 990-truth: is what a report page SAYS true? (a1, 2026-10-16)

Viken found the holes one page at a time: the "990 data (CSV)" link served the PDF (V1b), the
footer said "does not rate, score, or rank" above cards that grade (V1d), Schedules filed
missed A/F/M/J/O (V1a), "+38 more" staff rows hid behind a header count, "k h" and ALL-CAPS
names, street addresses. Each was a pattern across thousands of pages. Sentry asks "does the
page load"; this asks "is it true". Every check is a pure function over the page's HTML (and
the IRS XML when given), so a test can pin it and a 10-14 render can prove it.

Usage:
  audit_990_truth.py --html page.html [--xml return.xml] [--url URL]
  audit_990_truth.py --url https://philanthropy.org/990/report/<ein>/<slug> [--xml return.xml]
    (live: sends x-atlas-test from $ATLAS_TEST_BYPASS, and opens every download link; with
    --html, --url is the base the CSV link is opened against)
Prints one JSON finding per line: {"check", "url", "evidence"}. Exit 0 always; a finding is
data for audit_file.py, not a crash.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import sys
import urllib.request
from xml.etree import ElementTree as ET

_A = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF = re.compile(r'href="([^"]*)"')
_TAGS = re.compile(r"<[^>]+>")
_NO_RATING = re.compile(r"(does not|do not|never)\s+(rate|score|grade)", re.I)
_GRADES = re.compile(r">\s*(Looks good|Worth a look|Concern|Fine|Strong|Weak)\s*<")
_MORE = re.compile(r"\+\s?(\d+)\s+more\b")
_STREET = re.compile(
    r"\b\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln(?!\s*\d)|Lane|Way|Pkwy|Hwy)\b\.?")
_IRS_WORDS = {"Occupancy"}  # IRS expense line names that read like a street name
_PERSONAL_MAIL = re.compile(r"[\w.+-]+@(?:gmail|yahoo|hotmail|aol|outlook|icloud)\.com", re.I)
_PERSON = re.compile(r'<a\b[^>]*href="/990/person/[^"]*"[^>]*>(.*?)</a>', re.S)
_SCH_ON = re.compile(r'class="sch on"[^>]*>\s*([A-Z]{1,3})\s')


def _text(fragment: str) -> str:
    return htmllib.unescape(_TAGS.sub(" ", fragment)).strip()


def links(page: str) -> list[tuple[str, str]]:
    """(href, visible text) for every link on the page."""
    out = []
    for attrs, inner in _A.findall(page):
        m = _HREF.search(attrs)
        if m:
            out.append((htmllib.unescape(m.group(1)), " ".join(_text(inner).split())))
    return out


def csv_link_is_pdf(page: str, fetch=None) -> list[str]:
    """V1b: a link labelled CSV must open a CSV. Judged by what the link serves after every
    redirect (Content-Type and first bytes), never by its href: /get?fy=..&fmt=csv is fine.
    fetch(href) -> (status, content type, first bytes); without one there is nothing to judge."""
    if fetch is None:
        return []
    bad = []
    for h, t in sorted(set(links(page))):
        if "CSV" not in t:
            continue
        status, ctype, head = fetch(h)
        if head.startswith(b"%PDF") or "pdf" in ctype.lower():
            bad.append(f'"{t}" -> {h} serves a PDF')
        elif status != 200 or "csv" not in ctype.lower():
            bad.append(f'"{t}" -> {h} serves {status} {ctype or "no type"}, not a CSV')
    return bad


def disclaimer_vs_grades(page: str) -> list[str]:
    """V1d: the copy says we do not rate while the cards grade."""
    body = _text(page)
    claim = _NO_RATING.search(body)
    grades = _GRADES.findall(page)
    if claim and grades:
        start = max(0, claim.start() - 40)
        return [f'says "...{body[start:claim.end() + 30].strip()}..." and shows '
                f"{len(grades)} grade words ({', '.join(sorted(set(grades)))})"]
    return []


def hidden_rows(page: str) -> list[str]:
    """YMCA: '+38 more' rows the reader cannot see behind a header count."""
    return [f"+{n} more" for n in _MORE.findall(_text(page)) if int(n) >= 5]


def garbage_names(page: str) -> list[str]:
    """'k h' (initials only) and ALL-CAPS names on person links."""
    bad = []
    for raw in _PERSON.findall(page):
        name = " ".join(_text(raw).split())
        parts = name.replace(".", "").split()
        if parts and all(len(p) <= 1 for p in parts):
            bad.append(f"initials only: {name!r}")
        elif len(name) > 3 and name.isupper():
            bad.append(f"ALL CAPS: {name!r}")
    return bad


def privacy_leaks(page: str) -> list[str]:
    """Street addresses and personal mail on a public page (city and state only)."""
    nodes = [htmllib.unescape(n) for n in _TAGS.split(page)]  # one rendered text node at a time
    streets = [m.group(0) for n in nodes for m in _STREET.finditer(n)]
    return ([f"street address: {st!r}" for st in streets if not _IRS_WORDS & set(st.split())]
            + [f"personal email: {m.group(0)!r}" for n in nodes
               for m in _PERSONAL_MAIL.finditer(n)])


def xml_schedules(xml_text: str) -> set[str]:
    """Schedule letters the return actually filed, read off the IRS XML element names."""
    root = ET.fromstring(xml_text)
    out = set()
    for el in root.iter():
        m = re.match(r"(?:\{[^}]*\})?IRS990Schedule([A-Z])$", el.tag)
        if m:
            out.add(m.group(1))
    return out


def schedules_vs_xml(page: str, xml_text: str) -> list[str]:
    """V1a: Schedules filed on the page must equal the schedules in the XML."""
    shown = set(_SCH_ON.findall(page))
    filed = xml_schedules(xml_text)
    out = []
    if filed - shown:
        out.append(f"filed in the XML, not shown as filed: {sorted(filed - shown)}")
    if shown - filed - {"III"}:
        out.append(f"shown as filed, not in the XML: {sorted(shown - filed - {'III'})}")
    return out


CHECKS = {
    "disclaimer_vs_grades": disclaimer_vs_grades,
    "hidden_rows": hidden_rows,
    "garbage_names": garbage_names,
    "privacy_leaks": privacy_leaks,
}


def audit(page: str, url: str = "", xml_text: str | None = None, fetch=None) -> list[dict]:
    found = [{"check": name, "url": url, "evidence": ev}
             for name, fn in CHECKS.items() for ev in fn(page)]
    found += [{"check": "csv_link_is_pdf", "url": url, "evidence": ev}
              for ev in csv_link_is_pdf(page, fetch)]
    if xml_text:
        found += [{"check": "schedules_vs_xml", "url": url, "evidence": ev}
                  for ev in schedules_vs_xml(page, xml_text)]
    return found


def _get(url: str, n: int | None = None) -> tuple[int, str, bytes]:
    """GET url, following redirects; (final status, Content-Type, body or its first n bytes)."""
    headers = {"User-Agent": "Mozilla/5.0 (fleet auditor)"}
    if os.environ.get("ATLAS_TEST_BYPASS"):
        headers["x-atlas-test"] = os.environ["ATLAS_TEST_BYPASS"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(n)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), b""


def fetcher(url: str):
    """fetch(href) for csv_link_is_pdf: opens a site-relative href against url's origin."""
    base = re.match(r"https?://[^/]+", url).group(0)
    return lambda href: _get(href if href.startswith("http") else base + href, 512)


def live_links(page: str, url: str) -> list[dict]:
    """Every PDF and XML link answers 200 with the type its label promises (CSV:
    csv_link_is_pdf)."""
    fetch = fetcher(url)
    out = []
    for href, text in sorted(set(links(page))):
        if not re.search(r"\b(PDF|XML)\b", text) or not href.startswith("/"):
            continue
        status, ctype, _ = fetch(href)
        want = re.search(r"\b(PDF|XML)\b", text).group(1).lower()
        if status != 200 or want not in ctype.lower():
            out.append({"check": "download_link_type", "url": url,
                        "evidence": f'"{text}" -> {href}: {status} {ctype or "no type"}'})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--html")
    ap.add_argument("--xml")
    ap.add_argument("--url", default="")
    a = ap.parse_args(argv)
    xml_text = open(a.xml).read() if a.xml else None
    fetch = fetcher(a.url) if a.url else None
    if a.html:
        page = open(a.html).read()
        found = audit(page, a.url, xml_text, fetch)
    else:
        status, _, body = _get(a.url)
        if status != 200:
            print(json.dumps({"check": "page_status", "url": a.url, "evidence": str(status)}))
            return 0
        page = body.decode("utf-8", "replace")
        found = audit(page, a.url, xml_text, fetch) + live_links(page, a.url)
    for f in found:
        print(json.dumps(f))
    print(f"audit_990_truth: {len(found)} finding(s) on {a.url or a.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
