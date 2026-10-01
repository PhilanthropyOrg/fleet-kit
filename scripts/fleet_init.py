#!/usr/bin/env python3
"""fleet_init.py -- start a new product on an EMPTY repo, in one command.

  python3 scripts/fleet_init.py --name shop --repo https://github.com/acme/shop \\
      --domain shop.example.com --brief ~/brief.md [--webhook-url https://box.example.com/webhook]

THE GAP. up.sh starts a fleet on any repo, but an empty repo gives it nothing to work from:
no vision, no goals of its own (the kit's scripts/okr.json holds another product's), no
issues, no rule that a PR needs review before it merges. This does the repo side, then hands
the box side to up.sh unchanged. Nothing here knows any one product.

What it does, in order. Every step looks first and only writes what is missing, so running it
twice changes nothing the second time:

  1. Reads the founder's brief (docs/founder-brief.template.md) and stops if a section is
     missing -- before anything is written anywhere.
  2. EMPTY repo only: commits docs/VISION.md (the brief) and fleet/okr.json (the one number,
     in the shape scripts/okr.py reads) and pushes them as the first commit. A repo that
     already has commits is never pushed to.
  3. Turns on auto-merge and protects the default branch: a PR needs judge-judy's
     `fleet-code-review` status (plus any --require-check). CI is NOT required on day one --
     it does not exist yet, and a rule naming a check that never runs deadlocks every PR.
  4. Files the founding issues through the board's one filing door, each with the labels,
     Vision-link and Given/When/Then criteria gru's gates need, and a `Blocked by #N` line
     gate_drops.py holds it on until the one it needs has closed.
  5. --webhook-url: creates the GitHub webhook with this instance's secret
     (instances/<name>/webhook_secret, the file up.sh mounts for webhook_receiver.py). The
     secret goes to gh on stdin; it is never printed and never on a command line.
  6. Starts the fleet: `up.sh --repo <url> --name <name>` (skip with --no-start).
  7. Prints what only a person can still do.

--dry-run reads, prints what it would write, and writes nothing: no push, no issue, no
setting, no secret file, no container.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(HERE))

REVIEW_CHECK = "fleet-code-review"   # the commit status judge-judy posts on every PR head
WEBHOOK_EVENTS = ["workflow_run", "pull_request"]   # the two webhook_receiver.py acts on
MARKER = "<!-- fleet-founding: {key} -->"           # gate_drops.FOUNDING_MARKER is its prefix
MARKER_RE = re.compile(r"<!-- fleet-founding: ([a-z_]+) -->")
SECTIONS = ("Business", "Customer", "How it makes money", "The one number", "Out of scope")
TEMPLATE = KIT / "docs" / "founder-brief.template.md"


class Stop(Exception):
    """A reason to stop that the founder can act on. Printed as one plain line."""


# --- the founder's brief --------------------------------------------------------------------

def _sections(text: str) -> tuple[str, dict[str, str]]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    title = ""
    out: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = ""
        elif cur is not None:
            out[cur] += line + "\n"
    return title, {k: v.strip() for k, v in out.items()}


def parse_brief(text: str, template: str = "") -> dict:
    """The brief as {name, sections, metric, target, by, vision}. Raises Stop naming every
    section that is missing, empty, or still the template's own words."""
    title, got = _sections(text)
    t_title, t_got = _sections(template) if template else ("", {})
    bad = []
    if not title or title == t_title:
        bad.append("the first line must be `# <your product's name>`")
    for name in SECTIONS:
        if not got.get(name):
            bad.append(f"section `## {name}` is missing or empty")
        elif got[name] == t_got.get(name):
            bad.append(f"section `## {name}` still holds the template's text")
    num = got.get("The one number") or ""
    field = {k: (re.search(rf"^{k}:[ \t]*(\S.*)$", num, re.M | re.I) or [None, ""])[1].strip()
             for k in ("Metric", "Target", "By")}
    if num:
        for k, v in field.items():
            if not v:
                bad.append(f"`## The one number` needs a `{k}: ...` line")
        if field["By"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", field["By"]):
            bad.append("`By:` must be a date written YYYY-MM-DD")
        if field["Metric"] and not re.search(r"[a-z0-9]", field["Metric"].lower()):
            bad.append("`Metric:` must be words, e.g. `paying customers`")
    if bad:
        raise Stop("the brief is not ready:\n  - " + "\n  - ".join(bad))
    vision = re.sub(r"<!--.*?-->\n?", "", text, flags=re.S).strip() + "\n"
    return {"name": title, "sections": got, "metric": field["Metric"], "target": field["Target"],
            "by": field["By"], "vision": vision}


def build_okr(brief: dict) -> dict:
    """fleet/okr.json for this product, in scripts/okr.json's shape (okr.problem() checks it).
    One objective -- the founder's one number -- and no key results yet: those are the
    product's to add, by PR, once it knows its own funnel."""
    slug = re.sub(r"[^a-z0-9]+", "_", brief["metric"].lower()).strip("_")
    return {
        "_doc": ("This product's goals, read live by the fleet (fleet-kit scripts/okr.py). A "
                 "Vision-link on an issue must name one of these ids or say `none (maintenance)`. "
                 "Written once by fleet_init.py from docs/VISION.md; change both together, by PR. "
                 "`metric` is a console tile id, or null."),
        "objective": {"id": f"okr.{slug}",
                      "label": f"{brief['target']} {brief['metric']} by {brief['by']}",
                      "metric": None},
        "key_results": [],
    }


# --- the founding issues ---------------------------------------------------------------------

def founding_issues(product: str, domain: str, objective: dict) -> list[dict]:
    """The first issues of any product, in build order. `needs` is the founding issue that must
    close first: everything needs a stack and CI; nothing waits on the deploy, because the
    deploy is the one that needs a server login only the founder holds."""
    oid, goal = objective["id"], objective["label"]
    return [
        {"key": "stack", "needs": None,
         "title": "The repo has a chosen stack and an app that runs with one command",
         "why": ("The repo holds docs/VISION.md and nothing else. Pick the plainest, most boring stack that "
                 "can serve the business described there, and stand up the smallest app that runs."),
         "non_goals": ["no product features", "no sign-in", "no payments", "no deploy"],
         "criteria": [
             "Given a fresh clone, when a person runs the one command the README's `Run it` section names, "
             "then the app starts and `GET /` answers 200.",
             "Given the README, when a builder reads it, then it names the language, the web framework and "
             "the database, the one command to run and the one command to test, and one sentence on why "
             "each fits the business in docs/VISION.md.",
             "Given the running app, when `GET /health` is requested, then it answers 200 with the git SHA "
             "it was built from (the deploy driver's `current_sha` reads this).",
         ],
         "link": "nothing can move this number until there is an app to build on"},
        {"key": "ci", "needs": "stack",
         "title": "Every pull request is tested before it can merge",
         "why": ("Today a PR merges on review alone: no test runs anywhere. The fleet's builders also need a "
                 "local test command, and the kit's contract for it is `python3 scripts/tests_for_diff.py --run`."),
         "non_goals": ["no deploy step in this workflow", "no coverage targets", "no new product features"],
         "criteria": [
             "Given a pull request into the default branch, when it is opened or pushed to, then "
             "`.github/workflows/ci.yml` runs lint and the tests in one job named `test`.",
             "Given a change that breaks `GET /health`, when CI runs, then the `test` check is red.",
             "Given a builder's worktree, when `python3 scripts/tests_for_diff.py --run` is run, then it runs "
             "the tests near the diff with this repo's own test runner and exits 0 on pass, non-zero on "
             "failure, and 3 when the diff is too wide to scope.",
             "Given the merged PR, when the operator reads its body, then it names the exact check name "
             "(`test`) to add to branch protection: `fleet_init.py ... --require-check test`.",
         ],
         "link": "a product that breaks on every merge cannot hold customers"},
        {"key": "deploy", "needs": "ci",
         "title": f"A merge to the default branch goes live on {domain}",
         "why": (f"Nothing is served at {domain} yet. The kit ships no deployer on purpose; it ships a contract "
                 "of three functions (`current_sha`, `deploy <sha>`, `health`) in fleet-kit "
                 "`scripts/deploy_driver.md` (in the fleet's container: /fleet-kit/scripts/deploy_driver.md). "
                 "Read it first and implement exactly that."),
         "non_goals": ["no blue-green or canary", "no new server: use the one the founder names",
                       "no secret, key or address committed to the repo"],
         "criteria": [
             "Given `scripts/deploy_driver.sh current_sha`, when the site is up, then it prints the 40-char "
             f"SHA read from the live `https://{domain}/health`, never a guess; unreadable is empty output "
             "and a non-zero exit.",
             "Given `scripts/deploy_driver.sh deploy <sha>`, when it is run twice with the same SHA, then the "
             "second run is a no-op; when a deploy fails, then `current_sha` still reports the old SHA.",
             f"Given `scripts/deploy_driver.sh health`, when `https://{domain}/` does not answer 200, then it "
             "exits non-zero with a one-line reason.",
             "Given a merge to the default branch, when `.github/workflows/deploy.yml` runs, then it calls "
             "`deploy` then `health` and goes red if either fails (the fleet's webhook watches `ci.yml` and "
             "`deploy.yml` by those names).",
             "Given a missing server address or login, when the workflow runs, then it fails with one line "
             "naming the missing secret. The server login is the founder's to give: the PR body names each "
             "GitHub Actions secret, and the builder files ONE `--class credential` ask for them.",
             "Given the merged PR, when the operator reads its body, then it lists the lines to add to this "
             "instance's fleet.env: `FLEET_DEPLOY_DRIVER`, `FIXER_HEALTH_URL`, `FIXER_PAGE_URL`.",
         ],
         "link": "no customer can reach a product that is not live"},
        {"key": "landing", "needs": "ci",
         "title": f"A visitor to {domain} learns what {product} is and can ask to join",
         "why": ("The person in docs/VISION.md's `Customer` section lands on the home page cold. In ten seconds "
                 "they should know what this is, that it is for them, and how to get it."),
         "non_goals": ["no accounts", "no checkout", "no blog or extra pages"],
         "criteria": [
             "Given a first-time visitor, when they open `/`, then the page says what the product does for the "
             "customer in the words of docs/VISION.md, and shows one call to action.",
             "Given a visitor, when they submit their email in the call to action, then it is stored and the "
             "page confirms it; a second submit of the same email does not create a second row.",
             "Given a 375px-wide phone, when the page loads, then nothing scrolls sideways; the PR carries a "
             "screenshot at 375px and at 1280px.",
         ],
         "link": "the first page is where every customer starts"},
        {"key": "auth", "needs": "ci",
         "title": "A customer can sign up, sign in and sign out",
         "why": "Nothing can be sold to, or remembered about, a person the product cannot recognise.",
         "non_goals": ["no social sign-in", "no roles or teams", "no profile page beyond the email"],
         "criteria": [
             "Given a new visitor, when they sign up with an email, then an account exists and they are signed in.",
             "Given a signed-out visitor, when they request an account-only page, then they are sent to sign-in "
             "and the page's content is not in the response.",
             "Given a signed-in customer, when they sign out, then the session no longer opens account-only pages.",
             "Given the database, when it is read, then no password or sign-in link is stored in plain text.",
             "Given the README, when a tester reads it, then it says how to create a test account locally.",
         ],
         "link": "an account is the thing a customer pays from"},
        {"key": "payments", "needs": "auth",
         "title": "A signed-in customer can pay -- Stripe TEST mode only",
         "why": ("docs/VISION.md's `How it makes money` section names who pays and how much. Build that one "
                 "purchase end to end against Stripe's TEST mode. Taking real money is a one-way door and it "
                 "is the founder's alone: the fleet never asks for, stores or uses a live key."),
         "non_goals": ["NO live mode: no `sk_live_` / `pk_live_` key anywhere, for any reason",
                       "no refunds, coupons or invoices", "no second plan"],
         "criteria": [
             "Given a signed-in customer, when they pay with Stripe's test card 4242 4242 4242 4242, then "
             "their account shows as paid.",
             "Given a Stripe key that does not start with `sk_test_` or `pk_test_`, when the app starts or a "
             "checkout begins, then it refuses with a clear error and no charge is attempted.",
             "Given a Stripe webhook call with a bad signature, when it arrives, then it is rejected and no "
             "account changes.",
             "Given the repo, when it is searched, then no Stripe key of any kind is committed; keys are read "
             "from the environment, and the builder files ONE `--class credential` ask for the TEST keys.",
         ],
         "link": "this is the step that turns a visitor into the number"},
        {"key": "analytics", "needs": "landing",
         "title": f"The one number is counted and anyone can read it: {goal}",
         "why": ("The fleet steers by the product's own number. Until the product counts it, every ranking "
                 "is a guess."),
         "non_goals": ["no third-party tracker", "no dashboard beyond one page", "no personal data in the output"],
         "criteria": [
             "Given a visit, a sign-up and a payment, when each happens, then one row per event is stored in the "
             "product's own database with a time.",
             f"Given `GET /api/signals`, when it is requested, then it returns the current value of `{oid}` "
             "with its target and date, counted from those rows.",
             "Given no events yet, when `GET /api/signals` is requested, then it returns zero, not an error.",
         ],
         "link": "the number cannot be moved until it is measured"},
    ]


def issue_body(spec: dict, n: int, total: int, product: str, oid: str, needs_number: int | None) -> str:
    lines = [MARKER.format(key=spec["key"]),
             f"Founding issue {n} of {total} for {product} (filed by fleet-kit's fleet_init.py)."]
    if needs_number:
        lines.append(f"Blocked by #{needs_number}")
    lines += ["", "## Why", spec["why"], "", "## Non-goals"]
    lines += [f"- {x}" for x in spec["non_goals"]]
    lines += ["", "## Acceptance criteria"]
    lines += [f"{i}. {c}" for i, c in enumerate(spec["criteria"], 1)]
    lines += ["", f"Vision-link: {oid} -- {spec['link']}"]
    return "\n".join(lines) + "\n"


# --- GitHub payloads (pure) ------------------------------------------------------------------

def protection_payload(checks: list[str]) -> dict:
    """PUT repos/{o}/{r}/branches/{b}/protection -- the README's own install step 7."""
    return {"required_status_checks": {"strict": False, "contexts": checks},
            "enforce_admins": False, "required_pull_request_reviews": None, "restrictions": None}


def hook_payload(url: str, secret: str) -> dict:
    """POST repos/{o}/{r}/hooks."""
    return {"name": "web", "active": True, "events": WEBHOOK_EVENTS,
            "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}}


def slug_of(repo: str) -> str:
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", repo)
    if not m:
        raise Stop(f"--repo must be a GitHub URL (https://github.com/owner/name or git@github.com:owner/name.git), got {repo!r}")
    return f"{m.group(1)}/{m.group(2)}"


# --- the run ---------------------------------------------------------------------------------

class Init:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.dry = a.dry_run
        self.slug = slug_of(a.repo)
        self.todo: list[str] = []      # what only a person can still do, printed last
        self.instance = KIT / "instances" / a.name
        self.secret_file = self.instance / "webhook_secret"

    def say(self, msg: str) -> None:
        print(f"[fleet_init] {msg}", flush=True)

    def would(self, msg: str) -> None:
        print(f"[fleet_init] DRY RUN, not done: {msg}", flush=True)

    def gh(self, args: list[str], stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                                  **({"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}))
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["gh", *args], 124, "", f"gh timed out after {timeout}s")

    def git(self, args: list[str], cwd: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    # 1 -- before anything is written
    def preflight(self) -> dict:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", self.a.name):
            raise Stop("--name must be lowercase letters, digits, `-`, `_` or `.` (it names the container)")
        if not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", self.a.domain):
            raise Stop(f"--domain must be a bare host name like shop.example.com, got {self.a.domain!r}")
        if self.a.webhook_url and not self.a.webhook_url.startswith("https://"):
            raise Stop("--webhook-url must start with https://")
        if self.a.webhook_url and self.a.no_start:
            raise Stop("--webhook-url keeps its secret on the box that runs the fleet: run it there, without --no-start")
        try:
            text = Path(self.a.brief).expanduser().read_text()
        except OSError as exc:
            raise Stop(f"cannot read --brief {self.a.brief}: {exc}") from exc
        brief = parse_brief(text, TEMPLATE.read_text() if TEMPLATE.exists() else "")
        for tool in ("gh", "git"):
            if not shutil.which(tool):
                raise Stop(f"`{tool}` is not installed on this machine")
        if self.gh(["auth", "token"]).returncode != 0 and not os.environ.get("GH_TOKEN"):
            raise Stop("GitHub is not signed in on this machine. Run `gh auth login`, then run this again.")
        return brief

    # 2 -- the first commit, on an empty repo only
    def scaffold(self, brief: dict, work: Path) -> dict:
        """Returns the goals this product is ranked against (what is, or will be, in the repo)."""
        r = self.git(["ls-remote", "--heads", self.a.repo])
        if r.returncode != 0:
            raise Stop(f"cannot reach {self.a.repo}: {(r.stderr or '').strip()[:200]}. Create the repo "
                       f"first (`gh repo create {self.slug} --private`) and check this machine can push to it.")
        okr = build_okr(brief)
        if r.stdout.strip():   # the repo has commits: read, never push
            self.git(["clone", "--quiet", "--depth", "1", self.a.repo, str(work)])
            have = [p for p in ("docs/VISION.md", "fleet/okr.json") if (work / p).is_file()]
            if len(have) == 2:
                self.say("repo already has docs/VISION.md and fleet/okr.json -- left as they are")
                import okr as okr_mod
                try:
                    theirs = json.loads((work / "fleet/okr.json").read_text())
                except ValueError:
                    theirs = None
                why = okr_mod.problem(theirs)
                if why:
                    raise Stop(f"{self.slug}'s fleet/okr.json is not usable ({why}). Fix it there, then run this again.")
                return theirs
            missing = [p for p in ("docs/VISION.md", "fleet/okr.json") if p not in have]
            self.say(f"repo already has commits and no {' / '.join(missing)} -- this never pushes to a repo with history")
            self.todo.append(f"Add {' and '.join(missing)} to {self.slug} by pull request (the brief is your "
                             f"docs/VISION.md; fleet/okr.json is printed by `fleet_init.py --dry-run`). Until "
                             f"then the fleet ranks this repo against the kit's own goals file.")
            if self.dry:
                print(json.dumps(okr, indent=2))
            return okr
        files = {"docs/VISION.md": brief["vision"], "fleet/okr.json": json.dumps(okr, indent=2) + "\n"}
        if self.dry:
            self.would(f"first commit to the empty repo {self.slug}: {', '.join(files)}")
            print(files["fleet/okr.json"], end="")
            return okr
        branch = self.default_branch()
        work.mkdir(parents=True, exist_ok=True)
        self.git(["init", "--quiet", "-b", branch, str(work)])
        for rel, content in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(content)
        ident = [] if self.git(["config", "user.email"], cwd=str(work)).stdout.strip() else \
            ["-c", "user.name=fleet-init", "-c", "user.email=fleet-init@users.noreply.github.com"]
        self.git(["add", *files], cwd=str(work))
        c = self.git([*ident, "commit", "--quiet", "-m",
                      f"The vision and the one number for {brief['name']}\n\n"
                      "docs/VISION.md is the founder's brief; fleet/okr.json is the goal the fleet is "
                      "ranked against. Written by fleet-kit's fleet_init.py."], cwd=str(work))
        if c.returncode != 0:
            raise Stop(f"could not commit the first files: {(c.stderr or c.stdout).strip()[:200]}")
        p = self.git(["push", "--quiet", self.a.repo, f"HEAD:refs/heads/{branch}"], cwd=str(work))
        if p.returncode != 0:
            raise Stop(f"could not push the first commit to {self.slug}: {(p.stderr or '').strip()[:200]}")
        self.say(f"pushed the first commit to {self.slug} ({branch}): {', '.join(files)}")
        return okr

    def default_branch(self) -> str:
        r = self.gh(["api", f"repos/{self.slug}", "--jq", ".default_branch"])
        return (r.stdout or "").strip() if r.returncode == 0 and (r.stdout or "").strip() else "main"

    # 3 -- merge rules
    def merge_rules(self) -> None:
        want = [REVIEW_CHECK, *self.a.require_check]
        r = self.gh(["api", f"repos/{self.slug}", "--jq", ".allow_auto_merge"])
        if (r.stdout or "").strip() != "true":
            if self.dry:
                self.would(f"turn on auto-merge for {self.slug}")
            else:
                w = self.gh(["api", "-X", "PATCH", f"repos/{self.slug}", "--input", "-"],
                            stdin=json.dumps({"allow_auto_merge": True}))
                chk = self.gh(["api", f"repos/{self.slug}", "--jq", ".allow_auto_merge"])
                if w.returncode == 0 and (chk.stdout or "").strip() == "true":
                    self.say("auto-merge is on (the fleet merges a PR once its required checks are green)")
                else:
                    self.todo.append(f"Turn on auto-merge for {self.slug} (Settings -> General -> Allow "
                                     f"auto-merge). GitHub refused it here; on a private repo this needs a "
                                     f"paid plan. Without it no fleet PR ever merges.")
        branch = self.default_branch()
        base = f"repos/{self.slug}/branches/{branch}/protection"
        r = self.gh(["api", base])
        if r.returncode == 0:
            try:
                have = (json.loads(r.stdout).get("required_status_checks") or {}).get("contexts") or []
            except ValueError:
                have = []
            missing = [c for c in want if c not in have]
            if not missing:
                self.say(f"{branch} already requires {', '.join(want)}")
                return
            if self.dry:
                self.would(f"add required check(s) {', '.join(missing)} to {branch} (other rules untouched)")
                return
            w = self.gh(["api", "-X", "POST", f"{base}/required_status_checks/contexts", "--input", "-"],
                        stdin=json.dumps({"contexts": missing}))
            if w.returncode != 0:   # protected, but with no status-check rule to add to
                w = self.gh(["api", "-X", "PATCH", f"{base}/required_status_checks", "--input", "-"],
                            stdin=json.dumps({"strict": False, "contexts": have + missing}))
        else:
            if self.dry:
                self.would(f"protect {branch}: a PR needs {', '.join(want)} before it merges")
                return
            w = self.gh(["api", "-X", "PUT", base, "--input", "-"], stdin=json.dumps(protection_payload(want)))
        if w.returncode == 0:
            self.say(f"{branch} now requires {', '.join(want)} before a PR merges")
        else:
            self.todo.append(f"Protect {branch} on {self.slug} so a PR needs `{REVIEW_CHECK}` (GitHub refused: "
                             f"{(w.stderr or w.stdout).strip()[:120]}). Needs repo admin; on a private repo, a "
                             f"paid plan. Then run this again.")

    # 4 -- the founding issues
    def issues(self, brief: dict, okr: dict) -> None:
        import board_github
        import issue_cluster
        specs = founding_issues(brief["name"], self.a.domain, okr["objective"])
        r = self.gh(["issue", "list", "--repo", self.slug, "--state", "all", "--limit", "1000",
                     "--json", "number,title,body,labels,state"], timeout=120)
        if r.returncode != 0:
            raise Stop(f"cannot read {self.slug}'s issues: {(r.stderr or '').strip()[:200]}")
        existing = json.loads(r.stdout or "[]")
        number = {m.group(1): i["number"] for i in existing for m in [MARKER_RE.search(i.get("body") or "")] if m}
        new = [s for s in specs if s["key"] not in number]
        if not new:
            self.say(f"all {len(specs)} founding issues are already filed")
            return
        if self.dry:
            for s in new:
                self.would(f"file founding issue: {s['title']}")
            return
        # The gates read the goals through okr.py; point it at THIS product's for the self-check.
        keep = {k: os.environ.get(k) for k in ("FLEET_OKR_FILE", "GH_REPO")}
        with tempfile.TemporaryDirectory(prefix="fleet-init-") as tmp:
            (Path(tmp) / "okr.json").write_text(json.dumps(okr))
            os.environ["FLEET_OKR_FILE"] = str(Path(tmp) / "okr.json")
            os.environ["GH_REPO"] = self.slug      # board_github's label calls carry no --repo
            try:
                self._file(specs, number, existing, brief, okr, board_github, issue_cluster)
            finally:
                for k, v in keep.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def _file(self, specs, number, existing, brief, okr, board_github, issue_cluster) -> None:
        board_github.ensure_labels("high")
        labels = [board_github.LABEL_BACKLOG, f"{board_github.PREFIX}priority-high", "quality:solid"]
        self.gh(["label", "create", "quality:solid", "--repo", self.slug, "--color", "0e8a16",
                 "--description", "the default bar: correct, tested, reviewed"])
        open_issues = [i for i in existing if i.get("state") == "OPEN"]
        for n, s in enumerate(specs, 1):
            if s["key"] in number:
                continue
            body = issue_body(s, n, len(specs), brief["name"], okr["objective"]["id"],
                              number.get(s["needs"]) if s["needs"] else None)
            gaps = issue_cluster.spec_gaps(body, labels)
            if gaps:   # a founding issue gru would drop is a bug here, not the founder's problem
                raise Stop(f"founding issue `{s['key']}` would be dropped by gru's gates: {gaps}")
            res = issue_cluster.file_issue(s["title"], body, labels, repo=self.slug, open_issues=open_issues)
            m = re.search(r"/issues/(\d+)\s*$", res.get("url") or "")
            if not res.get("ok") or not (m or res.get("number")):
                raise Stop(f"could not file `{s['title']}`: {res.get('error') or res}")
            number[s["key"]] = int(m.group(1)) if m else int(res["number"])
            self.say(f"filed #{number[s['key']]}: {s['title']}")

    # 5 -- the webhook
    def webhook(self) -> None:
        url = self.a.webhook_url
        if not url:
            self.todo.append("Optional, for instant reactions: give the box's port (view port + 1, printed by "
                             "up.sh) a public https address, then run this again with `--webhook-url "
                             "https://<that address>/webhook`. Without it the fleet polls every few minutes.")
            return
        r = self.gh(["api", f"repos/{self.slug}/hooks?per_page=100"])
        try:
            hooks = json.loads(r.stdout or "[]") if r.returncode == 0 else None
        except ValueError:
            hooks = None
        if hooks is None:
            self.todo.append(f"Could not read {self.slug}'s webhooks (needs repo admin). Sign in as an admin "
                             f"and run this again.")
            return
        if any((h.get("config") or {}).get("url") == url for h in hooks):
            self.say(f"webhook to {url} already exists")
            return
        if self.dry:
            self.would(f"create a GitHub webhook to {url} for {', '.join(WEBHOOK_EVENTS)}")
            return
        w = self.gh(["api", "-X", "POST", f"repos/{self.slug}/hooks", "--input", "-"],
                    stdin=json.dumps(hook_payload(url, self.secret())))
        if w.returncode == 0:
            self.say(f"webhook created: {url} ({', '.join(WEBHOOK_EVENTS)}); its secret is in {self.secret_file}")
        else:
            self.todo.append(f"GitHub refused the webhook ({(w.stderr or '').strip()[:120]}). Needs repo admin; "
                             f"then run this again.")

    def secret(self) -> str:
        """This instance's webhook secret: the same file, mode and length up.sh would make, so
        up.sh finds it there and never prints it."""
        if not self.secret_file.is_file() or not self.secret_file.read_text().strip():
            self.instance.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32) + "\n")
        return self.secret_file.read_text().strip()

    # 6 -- the box side is up.sh's, unchanged
    def start(self) -> None:
        accounts = (self.a.accounts or os.environ.get("FLEET_ACCOUNTS") or "primary").split()
        # up.sh's own check, made first: it mounts ~/.claude-<account> into the container.
        no_login = [x for x in accounts
                    if not any((Path.home() / f".claude-{x}" / f).is_file() for f in (".credentials.json", ".claude.json"))]
        no_dir = [x for x in no_login if not (Path.home() / f".claude-{x}").is_dir()]
        up = ["bash", str(KIT / "up.sh"), "--repo", self.a.repo, "--name", self.a.name,
              *(["--accounts", self.a.accounts] if self.a.accounts else [])]
        shown = " ".join(up[1:3] + [f'"{x}"' if " " in x else x for x in up[3:]])
        for x in no_login:
            self.todo.append(f"Sign the model account `{x}` in on this box: `claude` then `/login`, then "
                             f"`mkdir -p ~/.claude-{x} && cp ~/.claude/.credentials.json ~/.claude-{x}/`.")
        if self.a.no_start:
            self.todo.append(f"Start the fleet on the box that will run it: `{shown}`")
            return
        if not shutil.which("podman"):
            self.todo.append(f"Install podman on this box, then start the fleet: `{shown}`")
            return
        if no_dir:
            self.todo.append(f"Then start the fleet: `{shown}` (not started now: up.sh cannot mount a model "
                             f"login that is not there)")
            return
        if self.dry:
            self.would(f"start the fleet: {shown}")
            return
        self.secret()   # made here, quietly, so up.sh does not print a fresh one to the terminal
        self.say(f"starting the fleet: {shown}")
        if subprocess.run(up).returncode != 0:
            self.todo.append(f"up.sh failed (its own output is above). Fix that, then: `{shown}`")

    def run(self) -> int:
        brief = self.preflight()
        self.say(f"{brief['name']}: {self.slug} -> {self.a.domain}" + ("  (DRY RUN: nothing is written)" if self.dry else ""))
        with tempfile.TemporaryDirectory(prefix="fleet-init-") as tmp:
            okr = self.scaffold(brief, Path(tmp) / "repo")
        self.merge_rules()
        self.issues(brief, okr)
        self.webhook()
        self.start()
        self.todo += [
            f"Point DNS for {self.a.domain} at the server that will serve the product.",
            "When the deploy issue's PR asks for the server login, add those GitHub Actions secrets "
            "(the fleet never holds your server's keys until you give them).",
            "When the payments issue asks, give Stripe TEST keys only. Going live with real money is "
            "your call alone, later.",
            f"After the CI issue merges, make its check required: run this again with `--require-check test` "
            f"and set `FLEET_REQUIRED_CHECKS=test` in {self.instance / 'fleet.env'}.",
        ]
        print(f"\n[fleet_init] Goal: {okr['objective']['label']} (`{okr['objective']['id']}`)")
        print("[fleet_init] Still yours to do -- nothing else waits on you:")
        for i, t in enumerate(self.todo, 1):
            print(f"  {i}. {t}")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Start a new product on an empty repo, in one command.")
    ap.add_argument("--name", required=True, help="the instance name (also names the container)")
    ap.add_argument("--repo", required=True, help="the product's GitHub repo URL")
    ap.add_argument("--domain", required=True, help="where the product will be served, e.g. shop.example.com")
    ap.add_argument("--brief", required=True, help="the founder's brief (docs/founder-brief.template.md, filled in)")
    ap.add_argument("--webhook-url", default="", help="public https address of this box's webhook port, ending /webhook")
    ap.add_argument("--require-check", action="append", default=[], metavar="NAME",
                    help="a CI check to require as well (repeatable); use once the repo has CI")
    ap.add_argument("--accounts", default="", help="model accounts, passed to up.sh (default: primary)")
    ap.add_argument("--no-start", action="store_true", help="set the repo up only; do not run up.sh")
    ap.add_argument("--dry-run", action="store_true", help="read and print; write nothing anywhere")
    a = ap.parse_args(argv)
    try:
        return Init(a).run()
    except Stop as exc:
        print(f"[fleet_init] STOPPED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
