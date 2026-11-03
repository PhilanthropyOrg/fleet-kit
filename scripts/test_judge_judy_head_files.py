"""judge-judy's review can read the changed files, not only the hunks (PHIA-1451).

The review runs with no tools. With hunks alone, about 3 in 10 reviews on 2026-11-03 said
"I could not open the files" and scored blind. head_files.py puts each changed file, whole and
at the PR head, into the prompt.
"""

import importlib.util
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HERE = ROOT / "members" / "judge-judy"
SRC = (HERE / "judge-judy.sh").read_text()
spec = importlib.util.spec_from_file_location("head_files", HERE / "head_files.py")
hf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hf)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


class HeadFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.old_cwd = os.getcwd()
        git(self.repo, "init", "-q")
        lines = [f"line_{i} = {i}" for i in range(1, 61)]
        pathlib.Path(self.repo, "app.py").write_text("\n".join(lines) + "\n")
        pathlib.Path(self.repo, "gone.py").write_text("bye = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        lines[29] = "line_30 = 'changed'"
        pathlib.Path(self.repo, "app.py").write_text("\n".join(lines) + "\n")
        pathlib.Path(self.repo, "gone.py").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "head")
        self.sha = git(self.repo, "rev-parse", "HEAD").strip()
        self.diff = git(self.repo, "diff", "HEAD~1", "HEAD")
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_shows_code_far_from_the_hunk(self):
        self.assertNotIn("line_1 = 1\n", self.diff)  # the hunk alone does not carry it
        out = hf.render(self.diff, self.sha)
        self.assertIn("===== FILE app.py", out)
        self.assertIn("line_1 = 1\n", out)
        self.assertIn("line_60 = 60\n", out)
        self.assertIn("line_30 = 'changed'", out)

    def test_deleted_file_is_not_printed(self):
        out = hf.render(self.diff, self.sha)
        self.assertNotIn("FILE gone.py", out)

    def test_budget_names_what_did_not_fit(self):
        out = hf.render(self.diff, self.sha, budget=50)
        self.assertNotIn("line_1 = 1", out)
        self.assertIn("app.py (over the file budget", out)


class Wiring(unittest.TestCase):
    def test_review_fetches_head_files(self):
        self.assertIn('head_files.py" "$HEAD_SHA" "$FILES_BUDGET" < "$DIFF_FILE"', SRC)

    def test_prompt_carries_the_files_after_the_diff(self):
        prompt = re.search(r'\nPROMPT="(.*?)\n  # --output-format json', SRC, re.S).group(1)
        self.assertIn('$(cat "${DIFF_FILE}.files"', prompt)
        self.assertLess(prompt.index('$(cat "$DIFF_FILE")'), prompt.index('${DIFF_FILE}.files'))
        self.assertIn("You have no tools and no shell", prompt)

    def test_rubric_never_asks_a_tool_less_review_to_open_files(self):
        # 665d914 told the judge "Confirm it (open the file, run the one test)"; it has no tools,
        # so 23 of 66 reviews after it wrote "could not open" and scored Feasibility 1.
        rubric = (HERE / "value-rubric.md").read_text()
        self.assertNotIn("open the file", rubric)
        self.assertNotIn("run the one test", rubric)
        self.assertIn("no shell", rubric)


if __name__ == "__main__":
    unittest.main()
