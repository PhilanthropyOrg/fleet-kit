#!/usr/bin/env python3
"""north.py -- the fleet paddles where Reif is paddling. fk#1097.

Reif, 2026-09-16, after two accounts hit their weekly limit in two days: "the point is that I
want to maximize the impact of the tokens. maybe what we do is add weight on the things that
I am shipping personally -- my hacksolo runs, what I am deploying, my last set of prs, likely
point to the true north. so look at that, and then look at the okrs, and then look at whats
burning... in this way the fleet actually enhances and we are all paddling in the same
direction."

Three reads, one output, no model:

  1. What Reif shipped himself -- merged PRs in the product repo whose branch is not the
     fleet's (`member/...`), last 14 days. Each is attributed to a KR by a fixed keyword table
     (KR_WORDS) over its title and touched paths. That table is the whole "judgment" and it is
     here so anyone can audit it.
  2. The OKR -- scripts/okr.json, plus the funnel from the product's own signals endpoint when
     it answers (worst step names the KR that is leaking).
  3. Where the tokens went -- runs.jsonl, last 7 days: cost by KR (a run that carries a `pr`
     number inherits that PR's KR; a run that never opened a PR is "no PR: <member>").

  north.py write  -> $FLEET_LOG_DIR/NORTH.md, ending in one weight per KR id (sums to 1;
                     `none` is always 0). run_member.sh prepends it after HANDOFF.md; marie
                     ranks by it (marie.md Part C).

Every block degrades to an honest "unreadable: <why>" line, never a guess; an unreadable
Reif signal or funnel leaves the weights at the equal split, which is today's behaviour.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
NORTH = LOG_DIR / "NORTH.md"
KR_IDS = ("okr.traffic", "okr.clicks", "okr.conversion")

# The attribution table. Every entry is a regex anchored at a word start, so "GitHub" is not
# "hub" and "runners" is not "rank". NONE_WORDS is checked first: a PR about ci / docs / the
# fleet / the OKR text itself is maintenance even when its title mentions a claim.
NONE_WORDS = (r"\bci\b", r"\bworkflow", r"\bdeploy", r"\bfleet", r"\brunner", r"\bwatchdog", r"\bdocs\b",
              r"\btests?\b", r"\bsuperadmin", r"\bcron\b", r"\bmail jobs?\b", r"\bcharter", r"\bclaude\.md",
              r"\bagents\.md", r"\bvision", r"\bokr", r"\bobjective", r"\bkey results?\b", r"\bledger",
              r"\binventory", r"\bretire", r"\breview\b", r"\bmerge queue", r"\bpromote", r"\bpytest",
              r"\bpostgres", r"\bhack-solo", r"\bblacksmith", r"\bgithub-hosted")
KR_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("okr.conversion", (r"\bclaim", r"\bverif", r"\bhq\b", r"\bowner", r"\bonboard", r"\bsign-?in\b", r"\bmagic",
                        r"\bbadge", r"\bapprov", r"\bheld question", r"\bupload", r"\borg console", r"\binvite",
                        r"\battest", r"\bwelcome")),
    ("okr.clicks", (r"\bcta\b", r"\bfollow", r"\bscout\b", r"\borg page", r"\breport page", r"/990/report",
                    r"\bcontact", r"\bperson page", r"\bprofile", r"\bsalar", r"\bofficer", r"\bboard member",
                    r"\broster", r"\binquir", r"\bmessag")),
    ("okr.traffic", (r"\bsearch", r"\bseo\b", r"\bsitemap", r"\bindex", r"\bcrawl", r"\bpseo\b", r"\bhubs?\b",
                     r"\btypo", r"\bsuggest", r"\branking\b", r"\bgsc\b", r"\bdid-you-mean", r"\bspell",
                     r"\blighthouse", r"\bjob board", r"\bcanonical", r"\bmisspell")),
]
PRODUCT_PATH = ("src/", "templates/")


def _hits(patterns, text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def attribute(title: str, paths: list[str] | None = None) -> str:
    """KR id for a PR, or 'none'. The title decides (a person wrote it); paths break ties."""
    hay = (title or "").lower()
    if _hits(NONE_WORDS, hay):
        return "none"
    for kr, words in KR_WORDS:
        if _hits(words, hay):
            return kr
    paths = paths or []
    if paths and not any(p.startswith(PRODUCT_PATH) for p in paths):
        return "none"
    phay = re.sub(r"[_/.\-]+", " ", " ".join(paths).lower())  # routes_claim.py -> "routes claim py"
    for kr, words in KR_WORDS:
        if _hits(words, phay):
            return kr
    return "none"


def repo_slug() -> str:
    url = (os.environ.get("FLEET_REPO_URL") or "").strip()
    slug = url.rsplit("github.com", 1)[-1].lstrip(":/").removesuffix(".git").strip("/")
    return slug if slug.count("/") == 1 and all(slug.split("/")) else ""


def _run(cmd: list[str], timeout: int = 60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def reif_prs(slug: str, now: float, days: int = 14, run=_run) -> tuple[list[dict], str | None]:
    """Merged PRs not opened by the fleet, last `days`. (rows, error)."""
    if not slug:
        return [], "FLEET_REPO_URL does not name a GitHub repo"
    try:
        r = run(["gh", "pr", "list", "--repo", slug, "--state", "merged", "--limit", "150",
                 "--json", "number,title,headRefName,mergedAt,files"])
    except Exception as e:  # noqa: BLE001
        return [], f"gh pr list failed: {e}"
    if r.returncode != 0:
        return [], f"gh pr list rc={r.returncode}: {(r.stderr or '').strip()[:120]}"
    try:
        rows = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return [], "gh pr list returned non-JSON"
    since = now - days * 86400
    out = []
    for p in rows:
        if (p.get("headRefName") or "").startswith("member/"):
            continue
        try:
            ts = time.mktime(time.strptime(p.get("mergedAt", "")[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except (ValueError, TypeError):
            continue
        if ts < since:
            continue
        paths = [f.get("path", "") for f in (p.get("files") or [])]
        out.append({"number": p.get("number"), "title": p.get("title", ""), "merged_at": ts,
                    "kr": attribute(p.get("title", ""), paths)})
    return out, None


def fleet_prs(slug: str, now: float, days: int = 7, run=_run) -> dict[int, str]:
    """PR number -> KR for the fleet's own merged PRs, so a run's `pr` can inherit it."""
    if not slug:
        return {}
    try:
        r = run(["gh", "pr", "list", "--repo", slug, "--state", "merged", "--limit", "200",
                 "--json", "number,title,headRefName,mergedAt,files"])
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for p in rows:
        paths = [f.get("path", "") for f in (p.get("files") or [])]
        out[int(p.get("number") or 0)] = attribute(p.get("title", ""), paths)
    return out


def load_okr(path: pathlib.Path | None = None) -> dict:
    p = path or pathlib.Path(os.environ.get("FLEET_OKR_FILE") or HERE / "okr.json")
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_funnel(url: str | None = None, token: str | None = None) -> tuple[dict, str | None]:
    """The product's funnel + OKR readings, or ({}, why)."""
    url = url or os.environ.get("FLEET_FUNNEL_URL") or ""
    if not url:
        base = (os.environ.get("FLEET_NUMBER_URL") or "").rsplit("/api/", 1)[0]
        url = f"{base}/api/signals/funnel" if base else ""
    if not url:
        return {}, "no FLEET_FUNNEL_URL / FLEET_NUMBER_URL"
    token = token or os.environ.get("FLEET_NUMBER_TOKEN") or ""
    headers = {"User-Agent": "fleet-kit/north"}
    if token:
        headers["X-PM-Token"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()), None
    except Exception as e:  # noqa: BLE001
        return {}, f"{url} -> {type(e).__name__}: {str(e)[:80]}"


def worst_step_kr(funnel: dict) -> str | None:
    """Which KR the funnel's worst step belongs to. Steps are named by the product."""
    step = (funnel.get("funnel") or {}).get("worst_step") or funnel.get("worst_step") or ""
    if isinstance(step, dict):
        step = " ".join(str(step.get(k) or "") for k in ("from_key", "to_key", "from_label", "to_label"))
    step = str(step).lower()
    if not step:
        return None
    if "cta" in step or "click" in step:
        return "okr.clicks" if "impression" in step else "okr.conversion"
    if "page_viewed" in step or "submit" in step or "claim" in step:
        return "okr.conversion"
    if "search" in step or "visit" in step or "traffic" in step:
        return "okr.traffic"
    return None


def load_runs(path: pathlib.Path | None = None) -> list[dict]:
    p = path or (LOG_DIR / "runs.jsonl")
    rows = []
    try:
        for line in p.read_text(errors="ignore").splitlines()[-20000:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return rows


def burn_by_kr(runs: list[dict], pr_kr: dict[int, str], now: float, days: int = 7) -> tuple[dict[str, float], float]:
    """USD by KR (fleet PRs attributed; PR-less spend as 'no PR: <member>'), and the total."""
    since = now - days * 86400
    out: dict[str, float] = {}
    total = 0.0
    for r in runs:
        if (r.get("ts") or 0) < since:
            continue
        cost = float((r.get("tokens") or {}).get("cost_usd") or 0)
        if not cost:
            continue
        total += cost
        pr = r.get("pr")
        try:
            pr = int(str(pr).lstrip("#")) if pr else None
        except ValueError:
            pr = None
        key = pr_kr.get(pr) if pr else None
        if key is None:
            key = f"no PR: {r.get('member') or '?'}"
        out[key] = out.get(key, 0.0) + cost
    return out, total


def weights(reif: list[dict], leak_kr: str | None) -> dict[str, float]:
    """One weight per KR, summing to 1. Reif's shipped PRs carry 60%, the funnel's leak 40%;
    with neither readable it is the equal split (today's behaviour). `none` is always 0."""
    counts = {k: sum(1 for p in reif if p["kr"] == k) for k in KR_IDS}
    n = sum(counts.values())
    reif_part = {k: (counts[k] / n if n else 1 / 3) for k in KR_IDS}
    if leak_kr in KR_IDS:
        leak_part = {k: (1.0 if k == leak_kr else 0.0) for k in KR_IDS}
        w = {k: 0.6 * reif_part[k] + 0.4 * leak_part[k] for k in KR_IDS}
    else:
        w = reif_part
    s = sum(w.values()) or 1.0
    w = {k: round(v / s, 2) for k, v in w.items()}
    w["none"] = 0.0
    return w


def tier_for(weight: float) -> str:
    return "high" if weight >= 0.4 else "medium" if weight >= 0.2 else "low"


def render(reif: list[dict], reif_err: str | None, okr: dict, funnel: dict, funnel_err: str | None,
           burn: dict[str, float], burn_total: float, now: float) -> str:
    labels = {k["id"]: k["label"] for k in okr.get("key_results", [])}
    labels[(okr.get("objective") or {}).get("id", "okr.verified_claims")] = (okr.get("objective") or {}).get("label", "")
    leak = worst_step_kr(funnel)
    w = weights(reif, leak)
    out = [f"# NORTH -- where Reif is paddling (written {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))})",
           "", "## 1. What Reif shipped himself, last 14 days (not the fleet's PRs)"]
    if reif_err:
        out.append(f"unreadable: {reif_err}")
    elif not reif:
        out.append("nothing merged outside the fleet in 14 days")
    else:
        by = {k: [p for p in reif if p["kr"] == k] for k in (*KR_IDS, "none")}
        for k in (*KR_IDS, "none"):
            if by[k]:
                out.append(f"- **{k}** ({len(by[k])}): " + "; ".join(f"#{p.get('number', '?')} {str(p.get('title', ''))[:70]}" for p in by[k][:6])
                           + (" ..." if len(by[k]) > 6 else ""))
    out += ["", "## 2. The OKR, and where the funnel leaks"]
    obj = okr.get("objective") or {}
    if obj:
        out.append(f"Objective: **{obj.get('label', '')}** (`{obj.get('id', '')}`)")
    for k in okr.get("key_results", []):
        out.append(f"- `{k['id']}` {k['label']}")
    if funnel_err:
        out.append(f"funnel: unreadable: {funnel_err}")
    else:
        f = funnel.get("funnel") or funnel
        step = f.get("worst_step")
        if isinstance(step, dict):  # the product's shape: {from_label, to_label, lost, drop_pct}
            step = f"{step.get('from_label')} -> {step.get('to_label')}: {step.get('lost')} lost ({step.get('drop_pct')}%)"
        out.append(f"funnel worst step: **{step}** -> `{leak or 'unmapped'}`" if step else "funnel: no worst_step in the response")
        for key in ("cta_clicked", "page_viewed", "submitted"):
            if key in f:
                out.append(f"- {key}: {f[key]}")
    out += ["", f"## 3. Where the fleet's tokens went, last 7 days (${burn_total:,.0f})"]
    if not burn:
        out.append("unreadable: no priced runs in runs.jsonl")
    else:
        kr_spend = {k: burn.get(k, 0.0) for k in (*KR_IDS, "none")}
        for k in (*KR_IDS, "none"):
            if kr_spend[k]:
                out.append(f"- {k}: ${kr_spend[k]:,.0f} ({kr_spend[k] / burn_total:.0%})")
        no_pr = sorted(((k, v) for k, v in burn.items() if k.startswith("no PR:")), key=lambda x: -x[1])
        if no_pr:
            tot = sum(v for _, v in no_pr)
            out.append(f"- no PR at all: ${tot:,.0f} ({tot / burn_total:.0%}) -- " + ", ".join(f"{k[7:]} ${v:,.0f}" for k, v in no_pr[:6]))
    out += ["", "## Weights (marie ranks by these; a Vision-link on a heavier KR ranks higher)"]
    out.append(" · ".join(f"`{k}` {w[k]:.2f} -> {tier_for(w[k])}" for k in KR_IDS) + " · `none (maintenance)` 0 -> low")
    out.append("")
    out.append("Rule: an item's tier is the tier of its Vision-link KR above. `fleet:reif-priority` stays outside this. "
               "Spend on a KR Reif is not touching and the funnel is not leaking is spend that does not paddle.")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("write", help="write NORTH.md")
    w.add_argument("--no-gh", action="store_true", help="skip gh + network reads (prompt-time refresh)")
    w.add_argument("--stdout", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "write":
        ap.print_help()
        return 2
    now = time.time()
    slug = repo_slug()
    if a.no_gh:
        # Prompt-time: never block a pass on gh/network. Reuse the last full write if present.
        if NORTH.exists() and now - NORTH.stat().st_mtime < 12 * 3600:
            if a.stdout:
                sys.stdout.write(NORTH.read_text())
            return 0
        reif, reif_err = [], "no gh at prompt time and no NORTH.md younger than 12h"
        funnel, funnel_err = {}, "skipped at prompt time"
        pr_kr = {}
    else:
        reif, reif_err = reif_prs(slug, now)
        funnel, funnel_err = load_funnel()
        pr_kr = fleet_prs(slug, now)
    burn, total = burn_by_kr(load_runs(), pr_kr, now)
    text = render(reif, reif_err, load_okr(), funnel, funnel_err, burn, total, now)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NORTH.write_text(text)
    if a.stdout:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
