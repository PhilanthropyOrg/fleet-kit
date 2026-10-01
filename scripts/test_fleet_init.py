"""A new product starts from an EMPTY repo with one command, and gets its own goals.

End to end, nothing mocked inside fleet_init.py: a bare git repo stands in for the empty
product repo (git's own `url.insteadOf` maps https://github.com/ onto it), a fake `gh` on PATH
keeps GitHub's state in a file and records every call with its stdin, and a stub up.sh records
that the box side was handed over. Then the fleet's real readers are pointed at the result:
okr.py must return the founder's goal, and gate_drops.plan must find every founding issue
buildable -- an issue gru would drop is useless.

RED without the change: scripts/fleet_init.py and scripts/okr.py do not exist, and
gate_drops.plan has no founding order.
"""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

URL = "https://github.com/acme/shop"
BRIEF = """# Shopfront

<!-- a comment the founder left in -->

## Business

We sell a ready-made online shop to corner bakeries.

## Customer

A bakery owner who takes orders on paper and loses half of them.

## How it makes money

Each bakery pays 29 dollars a month.

## The one number

Metric: Paying shops
Target: 40
By: 2027-03-31

## Out of scope

No delivery, no mobile app.
"""

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_GH_DIR"]
sp = os.path.join(d, "state.json")
st = json.load(open(sp)) if os.path.exists(sp) else {"issues": [], "labels": [], "protection": None,
                                                     "hooks": [], "auto_merge": False}
a = sys.argv[1:]
stdin = sys.stdin.read() if "--input" in a else ""
open(os.path.join(d, "calls.jsonl"), "a").write(json.dumps({"argv": a, "stdin": stdin}) + "\n")
def save(): json.dump(st, open(sp, "w"))
def opt(name, default=None):
    return a[a.index(name) + 1] if name in a else default
def opts(name):
    return [a[i + 1] for i, x in enumerate(a) if x == name]
if a[:2] == ["auth", "token"]:
    print("gho_fake"); sys.exit(0)
if a[0] == "label":
    st["labels"].append(a[2]); save(); sys.exit(0)
if a[:2] == ["issue", "list"]:
    print(json.dumps([dict(i, labels=[{"name": x} for x in i["labels"]]) for i in st["issues"]])); sys.exit(0)
if a[:2] == ["issue", "create"]:
    n = len(st["issues"]) + 1
    st["issues"].append({"number": n, "title": opt("--title"), "body": opt("--body"),
                         "labels": opts("--label"), "state": "OPEN", "repo": opt("--repo")})
    save(); print(f"https://github.com/acme/shop/issues/{n}"); sys.exit(0)
if a[:2] == ["issue", "comment"]:
    st.setdefault("comments", []).append(a); save(); sys.exit(0)
if a[0] == "api":
    method = opt("-X", "GET")
    path = [x for x in a[1:] if x.startswith("repos/")][0].split("?")[0]
    jq = opt("--jq")
    body = json.loads(stdin) if stdin.strip() else None
    if path == "repos/acme/shop":
        if method == "PATCH": st["auto_merge"] = bool(body.get("allow_auto_merge")); save(); sys.exit(0)
        repo = {"default_branch": "main", "allow_auto_merge": st["auto_merge"]}
        print(json.dumps(repo[jq[1:]]) .strip('"') if jq else json.dumps(repo)); sys.exit(0)
    if path == "repos/acme/shop/branches/main/protection":
        if method == "PUT":
            st["protection"] = {"required_status_checks": body["required_status_checks"]}; save(); sys.exit(0)
        if st["protection"] is None:
            sys.stderr.write("gh: Branch not protected (HTTP 404)\n"); sys.exit(1)
        print(json.dumps(st["protection"])); sys.exit(0)
    if path.endswith("/required_status_checks/contexts") and method == "POST":
        st["protection"]["required_status_checks"]["contexts"] += body["contexts"]; save(); sys.exit(0)
    if path == "repos/acme/shop/hooks":
        if method == "POST":
            st["hooks"].append({"id": len(st["hooks"]) + 1, "config": {"url": body["config"]["url"]}}); save(); sys.exit(0)
        print(json.dumps(st["hooks"])); sys.exit(0)
sys.stderr.write("fake gh: unhandled " + " ".join(a) + "\n"); sys.exit(1)
'''

UP_STUB = '''#!/usr/bin/env bash
# stands in for up.sh: records the hand-off, and that the secret was already in place
echo "$@" >> "$(dirname "$0")/up_calls.txt"
[ -s "$(dirname "$0")/instances/shop/webhook_secret" ] && echo secret-present >> "$(dirname "$0")/up_calls.txt"
exit 0
'''

WRITE_WORDS = ("-X", "create", "comment", "edit", "close", "delete")


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)


class Box:
    """A throwaway kit checkout, home dir, PATH and empty remote."""

    def __init__(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fleet-init-test-"))
        self.kit = self.tmp / "kit"
        shutil.copytree(ROOT / "scripts", self.kit / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.kit / "docs").mkdir()
        shutil.copy(ROOT / "docs" / "founder-brief.template.md", self.kit / "docs")
        (self.kit / "up.sh").write_text(UP_STUB)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("gh", FAKE_GH), ("podman", "#!/bin/sh\nexit 0\n")):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.gh_dir = self.tmp / "gh"
        self.gh_dir.mkdir()
        self.home = self.tmp / "home"
        (self.home / ".claude-primary").mkdir(parents=True)
        (self.home / ".claude-primary" / ".credentials.json").write_text("{}")
        self.remote = self.tmp / "remotes" / "acme" / "shop"
        self.remote.mkdir(parents=True)
        git("init", "--quiet", "--bare", "-b", "main", str(self.remote))
        self.brief = self.tmp / "brief.md"
        self.brief.write_text(BRIEF)
        keep = {k: v for k, v in os.environ.items()
                if not k.startswith(("FLEET_", "GH_", "GIT_")) and k != "GITHUB_TOKEN"}
        self.env = dict(keep, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", HOME=str(self.home),
                        FAKE_GH_DIR=str(self.gh_dir), GIT_CONFIG_COUNT="1",
                        GIT_CONFIG_KEY_0=f"url.{self.tmp}/remotes/.insteadOf",
                        GIT_CONFIG_VALUE_0="https://github.com/", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_SYSTEM=os.devnull)

    def run(self, *extra, brief=None):
        return subprocess.run(
            [sys.executable, str(self.kit / "scripts" / "fleet_init.py"), "--name", "shop", "--repo", URL,
             "--domain", "shop.example.com", "--brief", str(brief or self.brief), *extra],
            env=self.env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)

    def calls(self):
        p = self.gh_dir / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def writes(self, calls=None):
        return [c for c in (self.calls() if calls is None else calls)
                if any(w in c["argv"] for w in WRITE_WORDS)]

    def state(self):
        return json.loads((self.gh_dir / "state.json").read_text())

    def show(self, path):
        return git("--git-dir", str(self.remote), "show", f"main:{path}").stdout

    def commits(self):
        r = git("--git-dir", str(self.remote), "rev-list", "--count", "main")
        return int(r.stdout) if r.returncode == 0 else 0

    def up_calls(self):
        p = self.kit / "up_calls.txt"
        return p.read_text().splitlines() if p.exists() else []


class RealRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Box()
        cls.first = cls.box.run("--webhook-url", "https://box.example.com/webhook")
        cls.calls_after_first = cls.box.calls()
        cls.second = cls.box.run("--webhook-url", "https://box.example.com/webhook")

    def test_first_run_succeeds_and_makes_the_first_commit(self):
        self.assertEqual(self.first.returncode, 0, self.first.stdout + self.first.stderr)
        self.assertEqual(self.box.commits(), 1)
        vision = self.box.show("docs/VISION.md")
        self.assertIn("We sell a ready-made online shop to corner bakeries.", vision)
        self.assertTrue(vision.startswith("# Shopfront"))
        self.assertNotIn("<!--", vision, "the template's own comments do not belong in the vision")

    def test_goals_file_is_the_founders_number_in_the_kits_shape(self):
        import okr
        goals = json.loads(self.box.show("fleet/okr.json"))
        self.assertIsNone(okr.problem(goals))
        self.assertEqual(goals["objective"], {"id": "okr.paying_shops", "label": "40 Paying shops by 2027-03-31",
                                              "metric": None})
        kit = json.loads((ROOT / "scripts" / "okr.json").read_text())
        self.assertEqual(set(goals) - {"_doc"}, set(kit) - {"_doc"}, "same top-level keys as the kit's own file")
        self.assertEqual(set(goals["objective"]), set(kit["objective"]))

    def test_the_fleets_own_loader_returns_the_founders_goal_from_that_repo(self):
        import okr
        clone = self.box.tmp / "clone"
        if not clone.exists():
            git("clone", "--quiet", str(self.box.remote), str(clone))
        old = {k: os.environ.pop(k, None) for k in ("FLEET_REPO", "FLEET_OKR_FILE")}
        os.environ["FLEET_REPO"] = str(clone)
        try:
            self.assertEqual(okr.ids(), ["okr.paying_shops"])
            self.assertEqual(okr.source(), clone / "fleet" / "okr.json")
            import north
            text = north.render([], None, north.load_okr(), {}, "skipped", {}, 0, 1_800_000_000.0)
            self.assertIn("40 Paying shops by 2027-03-31", text)
            self.assertIn("`okr.paying_shops` 1.00 -> high", text)
            self.assertNotIn("okr.traffic", text, "another product's key results must not reach this one")
            # and gru's gates, reading the same goals, find every founding issue buildable
            import gate_drops
            items = [{"number": i["number"], "body": i["body"], "comments": [],
                      "labels": [{"name": x} for x in i["labels"]]} for i in self.box.state()["issues"]]
            p = gate_drops.plan(items, "t")
            self.assertEqual(p["eligible"], [1, 2, 3, 4, 5, 6, 7], p["dropped"])
            self.assertEqual(p["actions"], [], "no label to add, no gap to comment on")
        finally:
            os.environ.pop("FLEET_REPO", None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_seven_founding_issues_filed_with_what_the_pick_path_needs(self):
        issues = self.box.state()["issues"]
        self.assertEqual([i["title"].split()[0] for i in issues], ["The", "Every", "A", "A", "A", "A", "The"])
        self.assertEqual(len(issues), 7)
        for i in issues:
            self.assertEqual(sorted(i["labels"]), ["fleet:backlog", "fleet:priority-high", "quality:solid"])
            self.assertEqual(i["repo"], "acme/shop")
            self.assertIn("Vision-link: okr.paying_shops -- ", i["body"])
            self.assertIn("## Acceptance criteria", i["body"])
        by = {i["body"].split("fleet-founding: ")[1].split(" ")[0]: i for i in issues}
        self.assertEqual(list(by), ["stack", "ci", "deploy", "landing", "auth", "payments", "analytics"])
        self.assertIn("scripts/deploy_driver.md", by["deploy"]["body"])
        for fn in ("current_sha", "deploy <sha>", "health"):
            self.assertIn(fn, by["deploy"]["body"])
        self.assertIn("TEST mode only", by["payments"]["title"])
        self.assertIn("NO live mode", by["payments"]["body"])
        self.assertIn("sk_test_", by["payments"]["body"])
        self.assertIn("scripts/tests_for_diff.py --run", by["ci"]["body"])

    def test_order_is_held_by_the_gate_not_by_hope(self):
        import gate_drops
        issues = self.box.state()["issues"]
        items = [{"number": i["number"], "body": i["body"], "comments": [],
                  "labels": [{"name": x} for x in i["labels"]]} for i in issues]
        closed: set[int] = set()

        def gh(cmd, timeout=60):
            n = int(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "CLOSED" if n in closed else "OPEN"}), "")

        def buildable():
            todo = [it for it in items if it["number"] not in closed]
            waits = gate_drops.founding_waits(todo, "acme/shop", run=gh)
            old = os.environ.get("FLEET_OKR_FILE")
            goals = self.box.tmp / "goals.json"
            goals.write_text(self.box.show("fleet/okr.json"))
            os.environ["FLEET_OKR_FILE"] = str(goals)
            try:
                p = gate_drops.plan(todo, "t", waits=waits)
            finally:
                os.environ.pop("FLEET_OKR_FILE") if old is None else os.environ.__setitem__("FLEET_OKR_FILE", old)
            held = {r["number"]: r for r in p["dropped"] if r["gate"] == "founding-order"}
            self.assertTrue(all(r["action"] == "by-design" for r in held.values()))
            self.assertEqual(p["actions"], [], "a held founding issue is never labeled needs-spec or commented on")
            return p["eligible"]

        self.assertEqual(buildable(), [1], "empty repo: only the stack issue; never seven builders at once")
        closed.add(1)
        self.assertEqual(buildable(), [2], "then CI")
        closed.add(2)
        self.assertEqual(buildable(), [3, 4, 5], "then deploy, landing and auth together")
        closed.update({4, 5})
        self.assertEqual(buildable(), [3, 6, 7], "payments and analytics do not wait on the deploy")

    def test_a_board_with_no_founding_issue_costs_no_call_and_plans_as_before(self):
        import gate_drops
        items = [{"number": 9, "body": "Blocked by #3\n\n## Acceptance\n- Given a visitor, when they open the "
                                       "page, then it loads.\n\nVision-link: none (maintenance)\n", "comments": [],
                  "labels": [{"name": "quality:solid"}]}]

        def gh(cmd, timeout=60):
            raise AssertionError(f"an ordinary issue must not cost a gh call: {cmd}")

        self.assertEqual(gate_drops.founding_waits(items, None, run=gh), {})
        self.assertEqual(gate_drops.plan([dict(i) for i in items], "t"),
                         gate_drops.plan([dict(i) for i in items], "t", waits={}))
        self.assertEqual(gate_drops.plan(items, "t")["eligible"], [9], "a plain `Blocked by` line holds nothing")

    def test_merge_rules_review_required_and_no_ci_check_that_does_not_exist_yet(self):
        st = self.box.state()
        self.assertTrue(st["auto_merge"])
        self.assertEqual(st["protection"]["required_status_checks"], {"strict": False, "contexts": ["fleet-code-review"]})
        put = [c for c in self.calls_after_first if "PUT" in c["argv"]]
        self.assertEqual(len(put), 1)
        self.assertIn("repos/acme/shop/branches/main/protection", put[0]["argv"])
        self.assertEqual(json.loads(put[0]["stdin"]),
                         {"required_status_checks": {"strict": False, "contexts": ["fleet-code-review"]},
                          "enforce_admins": False, "required_pull_request_reviews": None, "restrictions": None})

    def test_webhook_created_with_the_instance_secret_and_the_secret_never_shown(self):
        post = [c for c in self.calls_after_first if c["argv"][:4] == ["api", "-X", "POST", "repos/acme/shop/hooks"]]
        self.assertEqual(len(post), 1)
        body = json.loads(post[0]["stdin"])
        secret_file = self.box.kit / "instances" / "shop" / "webhook_secret"
        secret = secret_file.read_text().strip()
        self.assertEqual(len(secret), 64)
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        self.assertEqual(body["config"], {"url": "https://box.example.com/webhook", "content_type": "json",
                                          "secret": secret, "insecure_ssl": "0"})
        self.assertEqual(body["events"], ["workflow_run", "pull_request"])
        for out in (self.first, self.second):
            self.assertNotIn(secret, out.stdout + out.stderr, "the secret must never be printed")
        for c in self.box.calls():
            self.assertNotIn(secret, " ".join(c["argv"]), "the secret must never be on a command line")

    def test_box_side_is_handed_to_up_sh_unchanged(self):
        self.assertEqual(self.box.up_calls()[:2], [f"--repo {URL} --name shop", "secret-present"])

    def test_remaining_human_steps_are_printed(self):
        out = self.first.stdout
        self.assertIn("Still yours to do", out)
        for words in ("Point DNS for shop.example.com", "Stripe TEST keys only", "--require-check test"):
            self.assertIn(words, out)

    def test_second_run_changes_nothing(self):
        self.assertEqual(self.second.returncode, 0, self.second.stdout + self.second.stderr)
        self.assertEqual(self.box.commits(), 1, "no second commit")
        self.assertEqual(len(self.box.state()["issues"]), 7, "no duplicate issues")
        self.assertEqual(len(self.box.state()["hooks"]), 1, "no second webhook")
        later = self.box.calls()[len(self.calls_after_first):]
        self.assertEqual(self.box.writes(later), [], "the second run made no GitHub write at all")
        self.assertIn("already filed", self.second.stdout)

    def test_nothing_of_another_product_reaches_the_new_one(self):
        blob = "\n".join([self.box.show("docs/VISION.md"), self.box.show("fleet/okr.json"), self.first.stdout]
                         + [i["title"] + i["body"] for i in self.box.state()["issues"]]).lower()
        for word in ("philanthrop", "nonprofit", "990", "verified_claims", "okr.traffic", "okr.clicks", "dino", "reif"):
            self.assertNotIn(word, blob)


class DryRun(unittest.TestCase):
    def test_dry_run_writes_nothing_anywhere(self):
        box = Box()
        r = box.run("--dry-run", "--webhook-url", "https://box.example.com/webhook")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(box.calls(), "it did read")
        self.assertEqual(box.writes(), [], "a dry run made a GitHub write")
        for c in box.calls():
            self.assertIn(c["argv"][0], ("auth", "api", "issue"))
            self.assertFalse(c["stdin"], "a dry run sent a body")
        self.assertEqual(box.commits(), 0, "a dry run pushed")
        self.assertEqual(git("--git-dir", str(box.remote), "for-each-ref").stdout, "")
        self.assertFalse((box.kit / "instances").exists(), "a dry run wrote a secret file")
        self.assertEqual(box.up_calls(), [], "a dry run started the fleet")
        self.assertIn('"id": "okr.paying_shops"', r.stdout)
        self.assertEqual(r.stdout.count("DRY RUN, not done: file founding issue"), 7)


class Refusals(unittest.TestCase):
    def test_an_unfinished_brief_stops_before_anything_is_touched(self):
        box = Box()
        bad = box.tmp / "bad.md"
        bad.write_text(BRIEF.replace("By: 2027-03-31", "By: next spring").replace(
            "## Out of scope\n\nNo delivery, no mobile app.\n", ""))
        r = box.run(brief=bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("`By:` must be a date written YYYY-MM-DD", r.stderr)
        self.assertIn("section `## Out of scope` is missing or empty", r.stderr)
        self.assertEqual(box.calls(), [])
        self.assertEqual(box.commits(), 0)

    def test_the_template_itself_is_not_a_brief(self):
        box = Box()
        r = box.run(brief=ROOT / "docs" / "founder-brief.template.md")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.count("still holds the template's text"), 5)

    def test_a_repo_with_history_is_never_pushed_to(self):
        box = Box()
        seed = box.tmp / "seed"
        git("init", "--quiet", "-b", "main", str(seed))
        (seed / "README.md").write_text("an existing product\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "existing", cwd=seed)
        git("push", "--quiet", str(box.remote), "main", cwd=seed)
        r = box.run("--no-start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(box.commits(), 1, "it pushed to a repo that already had commits")
        self.assertIn("never pushes to a repo with history", r.stdout)
        self.assertIn("Add docs/VISION.md and fleet/okr.json to acme/shop by pull request", r.stdout)
        self.assertEqual(box.up_calls(), [], "--no-start still started the fleet")


if __name__ == "__main__":
    unittest.main()
