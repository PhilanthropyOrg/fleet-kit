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
  3. Where the tokens went -- runs.jsonl, last 7 days: cost by KR (a run whose `pr`,
     `checkpoint_pr` or `item_id` is a merged fleet PR inherits its KR; one with a PR that has
     not merged is "PR not merged: <member>"; the rest are "no PR: <member>").

  north.py write  -> $FLEET_LOG_DIR/NORTH.md, ending in one weight per KR id (sums to 1;
                     `none` is always 0). run_member.sh prepends it after HANDOFF.md; marie
                     ranks by it (marie.md Part C).

Every block degrades to an honest "unreadable: <why>" line, never a guess; an unreadable
Reif signal or funnel leaves the weights at the equal split, which is today's behaviour.
"""
from __future__ import annotations

import calendar
import argparse
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from fleet_tz import stamp as central_stamp  # noqa: E402 -- humans read Central
import okr as okr_file  # noqa: E402 -- the one goals loader

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
    # gh#11787: the ladder after the claim is its own KR; checked first, so "verified badge"
    # lands here and not on conversion.
    ("okr.orgverify", (r"\borg ?verify", r"\bbadge", r"\bproof", r"\bprove", r"\battest", r"\bsite code",
                       r"\bdomain (?:e-?)?mail", r"\bsigned statement", r"\bid check", r"\bverified sheet",
                       r"\bsuperpage")),
    ("okr.conversion", (r"\bclaim", r"\bverif", r"\bhq\b", r"\bowner", r"\bonboard", r"\bsign-?in\b", r"\bmagic",
                        r"\bapprov", r"\bheld question", r"\bupload", r"\borg console", r"\binvite",
                        r"\bwelcome")),
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
            ts = calendar.timegm(time.strptime(p.get("mergedAt", "")[:19], "%Y-%m-%dT%H:%M:%S"))
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
    # 2026-09-30: --limit 200 with no date covered ~2.7 of the 7 days (514 fleet merges that
    # week), so a run on an older merged PR read as "no PR". Bound by date instead.
    since = time.strftime("%Y-%m-%d", time.gmtime(now - days * 86400))
    try:
        r = run(["gh", "pr", "list", "--repo", slug, "--state", "merged", "--limit", "1000",
                 "--search", f"merged:>={since}", "--json", "number,title,headRefName,mergedAt,files"],
                timeout=180)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for p in rows:
        paths = [f.get("path", "") for f in (p.get("files") or [])]
        out[int(p.get("number") or 0)] = attribute(p.get("title", ""), paths)
    return out


def load_okr(path: pathlib.Path | None = None) -> dict:
    """The goals, from okr.py: FLEET_OKR_FILE, else the product repo's fleet/okr.json, else the
    kit's scripts/okr.json. One loader, so NORTH.md and the Vision-link gate never disagree."""
    return okr_file.load(path)


def kr_ids_of(okr: dict) -> tuple[str, ...]:
    """The ids the weights are split over: the loaded goals' key results (a product with only
    an objective is weighed on that). No goals readable -> KR_IDS, as before."""
    ids = tuple(k["id"] for k in okr.get("key_results") or [] if isinstance(k, dict) and k.get("id"))
    obj = (okr.get("objective") or {}).get("id")
    return ids or ((obj,) if obj else KR_IDS)


FUNNEL_TIMEOUT_S = 45  # fk#1189: the live endpoint answered in 30.5s; 20s read as "unreadable"


def source_env_file(path: pathlib.Path | None = None) -> dict[str, str]:
    """`set -a; . fleet.env` for a Python process: every KEY=value in FLEET_ENV_FILE lands in
    os.environ unless the process already has that key. fk#1189: run outside run_member.sh (a
    hand `podman exec`, a debugging shell) north.py had no token at all and every funnel read
    said "unreadable" for a reason that was not the product's. Returns what it applied."""
    p = path or pathlib.Path(os.environ.get("FLEET_ENV_FILE") or HERE.parent / "fleet.env")
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return {}
    applied = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v
            applied[k] = v
    return applied


def load_funnel(url: str | None = None, token: str | None = None) -> tuple[dict, str | None]:
    """The product's funnel + OKR readings, or ({}, why). `why` names which of the four it was
    (no token / 401-403 / timeout / endpoint error) so an operator reading NORTH.md knows what
    to fix instead of guessing between a WAF rule, a missing env and a slow query (fk#1189)."""
    source_env_file()
    url = url or os.environ.get("FLEET_FUNNEL_URL") or ""
    if not url:
        base = (os.environ.get("FLEET_NUMBER_URL") or "").rsplit("/api/", 1)[0]
        url = f"{base}/api/signals/funnel" if base else ""
    if not url:
        return {}, "no FLEET_FUNNEL_URL / FLEET_NUMBER_URL"
    token = token or os.environ.get("FLEET_NUMBER_TOKEN") or ""
    if not token:
        return {}, f"{url} -> no token (FLEET_NUMBER_TOKEN unset; source FLEET_ENV_FILE)"
    headers = {"User-Agent": "fleet-kit/north", "X-PM-Token": token}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=FUNNEL_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {}, f"{url} -> {e.code} (token or waf)"
        return {}, f"{url} -> endpoint error: HTTP {e.code}"
    except (TimeoutError, socket.timeout):
        return {}, f"{url} -> timeout after {FUNNEL_TIMEOUT_S}s"
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)) or "timed out" in str(e.reason):
            return {}, f"{url} -> timeout after {FUNNEL_TIMEOUT_S}s"
        return {}, f"{url} -> endpoint error: {str(e.reason)[:80]}"
    except Exception as e:  # noqa: BLE001
        return {}, f"{url} -> endpoint error: {type(e).__name__}: {str(e)[:80]}"
    err = (body.get("errors") or {}).get("funnel") if isinstance(body, dict) else None
    if err:
        return {}, f"{url} -> endpoint error: {str(err)[:120]}"
    return body, None


def ladder(funnel: dict) -> dict:
    """gh#11787: the product's after-claim ladder (approved -> back in HQ -> OrgVerify started
    -> proved -> badge) when the feed carries one, else {}."""
    lad = funnel.get("ladder") if isinstance(funnel, dict) else None
    return lad if isinstance(lad, dict) and lad.get("stages") else {}


def worst_step_kr(funnel: dict) -> str | None:
    """Which KR the funnel's worst step belongs to. Steps are named by the product. gh#11787:
    while the feed carries the after-claim ladder, ITS worst step is the leak (Reif, 2026-10-08:
    "something has to drive it to do orgverify and get people through that funnel")."""
    if ladder(funnel).get("worst_step"):
        return "okr.orgverify"
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
        # 2026-09-30: only judge-judy and builder ever set `pr`. A minion's PR is its
        # `checkpoint_pr`, and the-fixer's `item_id` IS the PR it repaired -- read all three,
        # or 95% of the week reads as "no PR" when a third of it built a real one.
        prs = [_int(r.get("pr")), _int(r.get("checkpoint_pr"))]
        key = next((pr_kr[n] for n in (*prs, _int(r.get("item_id"))) if n in pr_kr), None)
        if key is None:
            key = f"{'PR not merged' if any(prs) else 'no PR'}: {r.get('member') or '?'}"
        out[key] = out.get(key, 0.0) + cost
    return out, total


def _int(v) -> int | None:
    try:
        return int(str(v).lstrip("#")) if v else None
    except ValueError:
        return None


def weights(reif: list[dict], leak_kr: str | None, ids: tuple[str, ...] = KR_IDS) -> dict[str, float]:
    """One weight per KR, summing to 1. Reif's shipped PRs carry 60%, the funnel's leak 40%;
    with neither readable it is the equal split (today's behaviour). `none` is always 0."""
    counts = {k: sum(1 for p in reif if p["kr"] == k) for k in ids}
    n = sum(counts.values())
    reif_part = {k: (counts[k] / n if n else 1 / len(ids)) for k in ids}
    if leak_kr in ids:
        leak_part = {k: (1.0 if k == leak_kr else 0.0) for k in ids}
        w = {k: 0.6 * reif_part[k] + 0.4 * leak_part[k] for k in ids}
    else:
        w = reif_part
    s = sum(w.values()) or 1.0
    w = {k: round(v / s, 2) for k, v in w.items()}
    w["none"] = 0.0
    return w


def tier_for(weight: float) -> str:
    return "high" if weight >= 0.4 else "medium" if weight >= 0.2 else "low"


def this_hour(funnel: dict) -> list[str]:
    """gh#11787, Reif 2026-10-08: "someone needs to make a plan hourly to drive the fleet to
    making stuff that achieves the target ... nothing says, given funnel, and everything we
    know, we should do this". These lines are that plan: the after-claim ladder's counts, the
    step losing the most orgs, and the one item the fleet builds for it. Empty when the feed
    carries no ladder, so a product without one reads exactly as before."""
    lad = ladder(funnel)
    if not lad:
        return []
    out = ["", f"## This hour: the ladder after the claim, last {lad.get('days', '?')} days"]
    out.append(" · ".join(f"{s.get('label')} {s.get('orgs')}" for s in lad["stages"]))
    step = lad.get("worst_step")
    if not step:
        out.append("no step loses anyone yet: build the next rung the ladder has no numbers for")
        return out
    out.append(f"Build this: close **{step.get('from_label')} -> {step.get('to_label')}** "
               f"({step.get('lost')} orgs lost, {step.get('drop_pct')}%) -> `okr.orgverify`")
    out.append("The item: the open `lane:orgverify` issue whose Vision-link is `okr.orgverify` and that closes "
               "this step. marie ranks it `fleet:priority-high` and files one when none is open; gru's "
               "`orgverify` standing lane builds it before anything else.")
    return out


def render(reif: list[dict], reif_err: str | None, okr: dict, funnel: dict, funnel_err: str | None,
           burn: dict[str, float], burn_total: float, now: float) -> str:
    labels = {k["id"]: k["label"] for k in okr.get("key_results", [])}
    labels[(okr.get("objective") or {}).get("id", "okr.verified_claims")] = (okr.get("objective") or {}).get("label", "")
    leak = worst_step_kr(funnel)
    ids = kr_ids_of(okr)
    w = weights(reif, leak, ids)
    out = [f"# NORTH -- where Reif is paddling (written {central_stamp('%Y-%m-%d %H:%M %Z', now)})",
           "", "## 1. What Reif shipped himself, last 14 days (not the fleet's PRs)"]
    if reif_err:
        out.append(f"unreadable: {reif_err}")
    elif not reif:
        out.append("nothing merged outside the fleet in 14 days")
    else:
        by = {k: [p for p in reif if p["kr"] == k] for k in (*ids, "none")}
        for k in (*ids, "none"):
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
        # the claim funnel's own KR on this line (gh#11787: `leak` may be the ladder's okr.orgverify)
        step_kr = worst_step_kr({"funnel": f})
        out.append(f"funnel worst step: **{step}** -> `{step_kr or 'unmapped'}`" if step else "funnel: no worst_step in the response")
        for key in ("cta_clicked", "page_viewed", "submitted"):
            if key in f:
                out.append(f"- {key}: {f[key]}")
        out += this_hour(funnel)
    out += ["", f"## 3. Where the fleet's tokens went, last 7 days (${burn_total:,.0f})"]
    if not burn:
        out.append("unreadable: no priced runs in runs.jsonl")
    else:
        kr_spend = {k: burn.get(k, 0.0) for k in (*ids, "none")}
        for k in (*ids, "none"):
            if kr_spend[k]:
                out.append(f"- {k}: ${kr_spend[k]:,.0f} ({kr_spend[k] / burn_total:.0%})")
        for prefix, label in (("PR not merged:", "a PR, not merged (yet)"), ("no PR:", "no PR at all")):
            rows = sorted(((k, v) for k, v in burn.items() if k.startswith(prefix)), key=lambda x: -x[1])
            if rows:
                tot = sum(v for _, v in rows)
                out.append(f"- {label}: ${tot:,.0f} ({tot / burn_total:.0%}) -- "
                           + ", ".join(f"{k[len(prefix) + 1:]} ${v:,.0f}" for k, v in rows[:6]))
    out += ["", "## Weights (marie ranks by these; a Vision-link on a heavier KR ranks higher)"]
    out.append(" · ".join(f"`{k}` {w[k]:.2f} -> {tier_for(w[k])}" for k in ids) + " · `none (maintenance)` 0 -> low")
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
    try:  # prepended to every pass's prompt: no credential (a PR title, an OKR note) may reach it
        import redact_secrets
        text = redact_secrets.safe_text(text)
    except Exception:  # noqa: BLE001
        pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NORTH.write_text(text)
    if a.stdout:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
