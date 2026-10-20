"""Desk 10-20: one 'connection reset by peer' failed judge-judy's whole queue scan (0 reviews for an
hour, six green PRs jammed). ghr retries each gh call 3x, 5 s apart. Mutation: drop the retry loop
or route a scan call around ghr -> RED."""
import re, subprocess, tempfile, unittest, os
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()


class GhRetry(unittest.TestCase):
    def test_ghr_succeeds_on_the_third_try(self):
        fn = re.search(r"^ghr\(\) \{.*\}$", SH, re.M).group(0)
        d = tempfile.mkdtemp(); c = os.path.join(d, "n")
        script = f'{fn}\nflaky() {{ n=$(cat {c} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {c}; [ $n -ge 3 ] && echo ok && return 0; return 1; }}\nGHR_SLEEP_S=0 ghr flaky'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout, "ok")

    def test_ghr_gives_up_after_three(self):
        fn = re.search(r"^ghr\(\) \{.*\}$", SH, re.M).group(0)
        r = subprocess.run(["bash", "-c", f"{fn}\nGHR_SLEEP_S=0 ghr false"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_scan_and_slug_go_through_ghr(self):
        pick = SH[SH.index("pick_pr() {"):SH.index("\n}\n", SH.index("pick_pr() {"))]
        self.assertNotRegex(pick, r"\$\((gh |timeout 25s gh )")
        self.assertIn("REPO_SLUG=$(ghr gh repo view", SH)


if __name__ == "__main__":
    unittest.main()
