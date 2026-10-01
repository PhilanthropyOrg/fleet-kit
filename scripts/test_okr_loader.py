"""One goals loader (scripts/okr.py), used by every reader, and safe for the live instance.

The kit's scripts/okr.json holds one product's goal. A product now carries its own in
<product repo>/fleet/okr.json. These pin:
  * an instance whose product repo has NO fleet/okr.json (the live one) gets the kit's file,
    byte for byte, and NORTH.md renders exactly as it did before;
  * FLEET_OKR_FILE still wins over everything, as before;
  * a product file wins over the kit's, for north.py AND the Vision-link gate alike;
  * a broken product file is passed over loudly -- it never raises and never empties the goals.

RED without the change: scripts/okr.py does not exist.
"""
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import north  # noqa: E402
import okr  # noqa: E402
import vision_link_gate as vlg  # noqa: E402

KIT = json.loads((ROOT / "scripts" / "okr.json").read_text())
MINE = {"objective": {"id": "okr.paying_shops", "label": "40 paying shops", "metric": None}, "key_results": []}


@contextlib.contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in ("FLEET_REPO", "FLEET_OKR_FILE")}
    for k in old:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in kv.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def product_repo(content: str | None) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    if content is not None:
        (d / "fleet").mkdir()
        (d / "fleet" / "okr.json").write_text(content)
    return d


class LiveInstanceIsUnchanged(unittest.TestCase):
    """The live product repo has no fleet/okr.json today."""

    def test_no_product_file_reads_the_kits_file(self):
        with env(FLEET_REPO=product_repo(None)):
            self.assertEqual(okr.source(), ROOT / "scripts" / "okr.json")
            self.assertEqual(okr.load(), KIT)
            self.assertEqual(north.load_okr(), KIT)
            self.assertEqual(vlg.kr_ids(), ["okr.verified_claims", "okr.traffic", "okr.clicks", "okr.conversion"])
        with env():
            self.assertEqual(okr.load(), KIT, "FLEET_REPO unset: the kit's file, nothing guessed")

    def test_north_md_is_byte_identical_to_main_for_the_kits_goals(self):
        """Render with this branch's north.py and with origin/main's, same inputs."""
        base = subprocess.run(["git", "-C", str(ROOT), "show", "origin/main:scripts/north.py"],
                              capture_output=True, text=True)
        if base.returncode != 0:
            self.skipTest("origin/main is not fetched here (a shallow CI checkout); run locally")
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "north_main.py").write_text(base.stdout)
        (d / "okr.json").write_bytes((ROOT / "scripts" / "okr.json").read_bytes())  # main reads the file beside it
        prog = ("import json,sys; sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[3]);"
                "n = __import__(sys.argv[1]);"
                "reif = [{'kr': 'okr.conversion', 'number': 7, 'title': 'Claim page'}] * 3 + [{'kr': 'okr.clicks', 'number': 8, 'title': 'CTA'}] + [{'kr': 'none', 'number': 9, 'title': 'ci'}];"
                "f = {'funnel': {'worst_step': 'cta_clicked->page_viewed', 'cta_clicked': 239}};"
                "b = {'okr.clicks': 3.0, 'none': 2.0, 'no PR: marie': 5.5, 'PR not merged: minion': 4.0};"
                "sys.stdout.write(n.render(reif, None, n.load_okr(), f, None, b, 14.5, 1_800_000_000.0));"
                "sys.stdout.write(n.render([], 'gh down', n.load_okr(), {}, 'skipped', {}, 0, 1_800_000_000.0));"
                "sys.stdout.write(n.render([], None, {}, {}, None, {}, 0, 1_800_000_000.0))")
        out = {}
        with env(FLEET_REPO=product_repo(None)):
            for mod, where in (("north_main", str(d)), ("north", str(ROOT / "scripts"))):
                r = subprocess.run([sys.executable, "-c", prog, mod, str(ROOT / "scripts"), where],
                                   capture_output=True, text=True, env=dict(os.environ))
                self.assertEqual(r.returncode, 0, r.stderr)
                out[mod] = r.stdout
        self.assertIn("`okr.conversion`", out["north"])
        self.assertEqual(out["north"], out["north_main"])

    def test_explicit_override_still_wins_over_a_product_file(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "o.json").write_text(json.dumps({"objective": {"id": "okr.env", "label": "x"}, "key_results": []}))
        with env(FLEET_OKR_FILE=d / "o.json", FLEET_REPO=product_repo(json.dumps(MINE))):
            self.assertEqual(okr.ids(), ["okr.env"])
        with env(FLEET_OKR_FILE=d / "missing.json"):
            self.assertEqual(okr.load(), {}, "an unreadable override reads as no goals, as before")
            self.assertEqual(vlg.kr_ids(), [])


class ProductGoals(unittest.TestCase):
    def test_every_reader_gets_the_product_repos_goals(self):
        repo = product_repo(json.dumps(MINE))
        with env(FLEET_REPO=repo):
            self.assertEqual(north.load_okr(), MINE)
            self.assertEqual(vlg.kr_ids(), ["okr.paying_shops"])
            self.assertEqual(vlg.classify_candidate("Vision-link: okr.paying_shops -- more shops", [])[0],
                             vlg.STATUS_LINKED)
            self.assertEqual(vlg.classify_candidate("Vision-link: okr.traffic -- someone else's", [])[0],
                             vlg.STATUS_MISSING, "another product's KR id is not a link here")
            cli = subprocess.run([sys.executable, str(ROOT / "scripts" / "okr.py"), "ids"],
                                 capture_output=True, text=True, env=dict(os.environ))
            self.assertEqual(cli.stdout.split(), ["okr.paying_shops"])

    def test_a_broken_product_file_falls_back_loudly_and_never_raises(self):
        for bad, why in (("{not json", "unreadable"), ("[]", "top level is not an object"),
                         (json.dumps({"objective": {"label": "no id"}}), "no objective.id"),
                         (json.dumps({"objective": {"id": "okr.x", "label": "x"}, "key_results": "nope"}),
                          "key_results is not a list")):
            repo = product_repo(bad)
            with env(FLEET_REPO=repo):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(okr.load(), KIT, bad)
                    self.assertEqual(north.load_okr(), KIT)
                    self.assertEqual(len(vlg.kr_ids()), 4)
                self.assertIn(str(repo / "fleet" / "okr.json"), err.getvalue())
                self.assertIn(why, err.getvalue())
                r = subprocess.run([sys.executable, str(ROOT / "scripts" / "north.py"), "write", "--no-gh", "--stdout"],
                                   capture_output=True, text=True,
                                   env=dict(os.environ, FLEET_LOG_DIR=tempfile.mkdtemp()))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("okr.verified_claims", r.stdout)
                self.assertIn("is not usable", r.stderr)

    def test_no_reader_opens_the_kits_goals_file_on_its_own(self):
        """A second reader with its own path is how two members end up with two sets of goals."""
        allowed = {"okr.py", "selftest.py"}   # selftest checks the KIT file against metrics.json
        opens = re.compile(r'"okr\.json"|environ\S*FLEET_OKR_FILE')
        own = [p.name for p in sorted((ROOT / "scripts").glob("*.py"))
               if p.name not in allowed and not p.name.startswith("test_") and opens.search(p.read_text())]
        self.assertEqual(own, ["fleet_init.py"], "fleet_init.py only sets FLEET_OKR_FILE for its own self-check")
        for charter in sorted((ROOT / "members").glob("*/*.md")):
            self.assertNotIn("/fleet-kit/scripts/okr.json", charter.read_text(),
                             f"{charter.name} points a member at the kit's goals file; use `okr.py ids`")


if __name__ == "__main__":
    unittest.main()
