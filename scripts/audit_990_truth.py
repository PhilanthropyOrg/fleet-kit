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
    (live: sends x-atlas-test from $ATLAS_TEST_BYPASS, and opens every CSV/PDF/XML link; with
    --html, --url is the base links are opened against. A link that lands on the Get-the-report
    page or sign-in is "gated", not a wrong file; with $QA_SESSION_TOKEN it is opened once more
    as the QA operator.)
Prints one JSON finding per line: {"check", "url", "evidence"}, then on stderr
"hits: N real, M gated, K other" (add these up for the pass). Exit 0 always; a finding is
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


_GATE = re.compile(r"/990/report/\d+/get/?(?:\?|$)|/(?:990/)?(?:login|sign-?in)\b", re.I)
_NOT_A_FILE = re.compile(r"\b(get|buy|order|unlock|sign in|log in)\b", re.I)  # labels of a gate


def served(ctype: str, head: bytes) -> str:
    """The file type a response really is, from its Content-Type and first bytes."""
    ctype = ctype.lower()
    if head.startswith(b"%PDF") or "pdf" in ctype:
        return "pdf"
    return next((k for k in ("csv", "xml") if k in ctype and "html" not in ctype), "")


def judge(text: str, href: str, want: str, fetch, signed_in=None) -> tuple[str, str]:
    """('clean'|'real'|'gated', evidence) for a link labelled `want` (csv, pdf or xml), by what
    it serves after every redirect, never by its href. A final page that is the Get-the-report
    page or a sign-in page is a gate, not a wrong file; with signed_in (the QA operator) the
    gate is followed once and the final response judged instead.
    fetch(href) -> (status, content type, first bytes, final URL)."""
    status, ctype, head, final = fetch(href)
    kind = served(ctype, head)
    if not kind and _GATE.search(final) and signed_in:
        status, ctype, head, final = signed_in(href)
        kind = served(ctype, head)
    if status == 200 and kind == want:
        return "clean", ""
    if not kind and _GATE.search(final):
        return "gated", f'"{text}" -> {href} lands on {re.sub(r"^https?://[^/]+", "", final)}'
    if kind == "pdf" and want == "csv":
        return "real", f'"{text}" -> {href} serves a PDF'
    return "real", f'"{text}" -> {href} serves {status} {ctype or "no type"}, not a {want.upper()}'


def download_hits(page: str, url: str, fetch, signed_in=None) -> tuple[list[dict], int]:
    """(findings, gated links) for every site link labelled CSV, PDF or XML (V1b: the CSV link
    must not open the PDF). A gated link is no finding unless its label promised a file; those
    are one finding per page."""
    real, gated, promised = [], 0, []
    for href, text in sorted(set(links(page))):
        m = re.search(r"\b(CSV|PDF|XML)\b", text)
        if not m or not href.startswith("/"):
            continue
        verdict, ev = judge(text, href, m.group(1).lower(), fetch, signed_in)
        if verdict == "real":
            check = "csv_link_is_pdf" if m.group(1) == "CSV" else "download_link_type"
            real.append({"check": check, "url": url, "evidence": ev})
        elif verdict == "gated":
            gated += 1
            if not _NOT_A_FILE.search(text):
                promised.append(ev)
    if promised:  # the CSV half is philanthropy#12783: dedupe there, never file it twice
        real.append({"check": "download_lands_on_gate", "url": url, "evidence":
                     f"CSV/PDF links land on a gate: {len(promised)} (see #12783): "
                     + "; ".join(promised)})
    return real, gated


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
    return leaks_in(htmllib.unescape(n) for n in _TAGS.split(page))  # one text node at a time


def leaks_in(texts) -> list[str]:
    """The privacy check on separate texts (page text nodes, JSON string values), one at a
    time, so a street never spans two fields."""
    nodes = list(texts)
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


def audit(page: str, url: str = "", xml_text: str | None = None) -> list[dict]:
    found = [{"check": name, "url": url, "evidence": ev}
             for name, fn in CHECKS.items() for ev in fn(page)]
    if xml_text:
        found += [{"check": "schedules_vs_xml", "url": url, "evidence": ev}
                  for ev in schedules_vs_xml(page, xml_text)]
    return found


def _headers() -> dict:
    h = {"User-Agent": "Mozilla/5.0 (fleet auditor)"}
    if os.environ.get("ATLAS_TEST_BYPASS"):
        h["x-atlas-test"] = os.environ["ATLAS_TEST_BYPASS"]
    return h


def _open(url: str, n: int | None = None, opener=None) -> tuple[int, str, bytes, str]:
    """GET url, following redirects; (final status, Content-Type, body or its first n bytes,
    final URL)."""
    try:
        with (opener or urllib.request.build_opener()).open(
                urllib.request.Request(url, headers=_headers()), timeout=25) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(n), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), b"", e.geturl()


def _get(url: str, n: int | None = None) -> tuple[int, str, bytes]:
    return _open(url, n)[:3]


def fetcher(url: str, opener=None):
    """fetch(href) for judge: opens a site-relative href against url's origin."""
    base = re.match(r"https?://[^/]+", url).group(0)
    return lambda href: _open(href if href.startswith("http") else base + href, 512, opener)


def signed_in_fetcher(url: str):
    """fetch as the QA operator (persona_law 10e), or None without $QA_SESSION_TOKEN or when
    the sign-in fails. Records the persona so audit_cleanup.py run resets it."""
    token = os.environ.get("QA_SESSION_TOKEN")
    if not token:
        return None
    base = re.match(r"https?://[^/]+", url).group(0)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    req = urllib.request.Request(
        base + "/990/api/qa/session", data=json.dumps({"user": "operator"}).encode(),
        headers={**_headers(), "x-qa-token": token, "Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=25) as r:
            link = json.load(r)["url"]
        opener.open(urllib.request.Request(link, headers=_headers()), timeout=25).close()
    except Exception as e:  # noqa: BLE001 -- signed out is still a pass, just say so
        print(f"audit_990_truth: QA operator sign-in failed ({type(e).__name__})", file=sys.stderr)
        return None
    import audit_cleanup
    audit_cleanup.record(audit_cleanup.state_path(), "personas", "operator")
    return fetcher(url, opener)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--html")
    ap.add_argument("--xml")
    ap.add_argument("--url", default="")
    a = ap.parse_args(argv)
    xml_text = open(a.xml).read() if a.xml else None
    if a.html:
        page = open(a.html).read()
    else:
        status, _, page = _get(a.url)
        if status != 200:
            print(json.dumps({"check": "page_status", "url": a.url, "evidence": str(status)}))
            return 0
        page = page.decode("utf-8", "replace")
    found = audit(page, a.url, xml_text)
    downloads, gated = (download_hits(page, a.url, fetcher(a.url), signed_in_fetcher(a.url))
                        if a.url else ([], 0))
    for f in found + downloads:
        print(json.dumps(f))
    print(f"audit_990_truth: {a.url or a.html} hits: {len(downloads)} real, {gated} gated, "
          f"{len(found)} other", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
