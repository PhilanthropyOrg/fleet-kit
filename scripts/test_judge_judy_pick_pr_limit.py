"""philanthropy 10-30 (PHIA-1192): 87 open PRs, and judge-judy reviewed only the newest. pick_pr's
`gh pr list` had no --limit, so gh handed back its default 30 newest PRs; the oldest-first sort
(fk#181) then ran over that window only. About 25 green armed heads below it never got a
fleet-code-review status, and an explicit `judge-judy.sh <pr>` for one of them found nothing to review.
Mutation: drop --limit from the list call -> RED."""
import os, re, subprocess, tempfile, unittest
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()

# Acts like gh: `pr list` returns open PRs newest first, cut to --limit (default 30).
GH_STUB = r"""#!/bin/bash
if [ "$1 $2" = "pr list" ]; then
  lim=30; while [ $# -gt 0 ]; do [ "$1" = "--limit" ] && lim=$2; shift; done
  python3 -c '
import json, sys
n = int(sys.argv[1]); lim = int(sys.argv[2])
prs = [{"number": i, "isDraft": False, "createdAt": "2026-10-01T%02d:%02d:00Z" % divmod(i, 60)} for i in range(1, n + 1)]
print(json.dumps(sorted(prs, key=lambda p: -p["number"])[:lim]))' "$OPEN_PRS" "$lim"
elif [ "$1 $2" = "pr view" ]; then echo "sha$3 "
elif [ "$1" = "api" ]; then echo "[]"
else exit 1; fi
"""


def pick(explicit, open_prs=87):
    fn = SH[SH.index("pick_pr() {"):SH.index("\n}\n", SH.index("pick_pr() {")) + 2]
    ghr = re.search(r"^ghr\(\) \{.*\}$", SH, re.M).group(0)
    d = tempfile.mkdtemp()
    Path(d, "gh").write_text(GH_STUB); os.chmod(Path(d, "gh"), 0o755)
    script = f'log() {{ :; }}\n{ghr}\n{fn}\nSETTLE_S=0 REQUIRED_CHECKS="" CONTEXT=fleet-code-review REPO_SLUG=o/r GHR_SLEEP_S=0\npick_pr "{explicit}" ""'
    env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", OPEN_PRS=str(open_prs))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


class PickPrSeesTheWholeQueue(unittest.TestCase):
    def test_tick_picks_the_oldest_of_87(self):
        r = pick("")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1 sha1")

    def test_explicit_pr_below_the_newest_30_is_found(self):
        r = pick("14")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "14 sha14")


if __name__ == "__main__":
    unittest.main()
