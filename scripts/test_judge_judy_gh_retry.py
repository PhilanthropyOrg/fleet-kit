"""Desk 10-20: one 'connection reset by peer' failed judge-judy's whole queue scan (0 reviews for an
hour, six green PRs jammed). ghr retries each gh call 3x, 5 s apart. Mutation: drop the retry loop
or route a scan call around ghr -> RED.

philanthropy 10-30: 87 open PRs, and judge-judy reviewed only the newest. pick_pr's `gh pr list` had
no --limit, so gh handed back its default 30 newest PRs; the oldest-first sort (fk#181) then ran over
that window only. About 25 green armed heads below it never got a fleet-code-review status, and an
explicit `judge-judy.sh <pr>` for one of them found nothing to review. Mutation: drop --limit from
the list call -> RED."""
import json, re, subprocess, tempfile, unittest, os
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
    def test_tick_picks_the_newest_of_87(self):
        r = pick("")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "87 sha87")

    def test_explicit_pr_below_the_newest_30_is_found(self):
        r = pick("14")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "14 sha14")


# philanthropy 10-30: a green PR for the company's top goal waited 3h20m for review behind 14 older
# heads. A PR labelled review:next goes first.
# PHIA-1451, desk 10-31: main red 2h, green revert 14855 waited behind 34 older PRs. Reverts and
# red-main fixes go ahead of everything; each lane is newest-created first.
# Mutation: drop labels or title from the list call, or a lane key from the sort -> RED.
PICK_SRC = SH[SH.index("pick_pr() {"):SH.index("report_run() {")]
ORDER = re.search(r"--arg p \"\$\{PRIORITY_LABEL-review:next\}\" '([^']+)'", PICK_SRC).group(1)


def lane(prs, label="review:next", explicit=""):
    out = subprocess.run(["jq", "-r", "--arg", "x", explicit, "--arg", "p", label, ORDER],
                         input=json.dumps(prs), capture_output=True, text=True, check=True).stdout
    return [int(x) for x in out.split()]


def _pr(n, created, labels=(), draft=False, title=""):
    return {"number": n, "createdAt": created, "isDraft": draft, "title": title,
            "labels": [{"name": l} for l in labels]}


QUEUE = [_pr(3, "2026-10-30T16:57:00Z", ["review:next"]), _pr(1, "2026-10-30T06:00:00Z"),
         _pr(2, "2026-10-30T07:00:00Z", ["bug"]), _pr(4, "2026-10-30T18:00:00Z", ["review:next"]),
         _pr(5, "2026-10-30T05:00:00Z", ["review:next"], draft=True)]


class PriorityLane(unittest.TestCase):
    def test_labelled_prs_go_first_then_newest_first(self):
        self.assertEqual(lane(QUEUE), [4, 3, 2, 1])

    def test_empty_label_is_the_plain_newest_first_queue(self):
        self.assertEqual(lane(QUEUE, label=""), [4, 3, 2, 1])

    def test_revert_and_red_main_fix_go_ahead_of_the_label_lane(self):
        q = QUEUE + [_pr(9, "2026-10-29T00:00:00Z", title="Deploys red: revert the report's net assets read"),
                     _pr(8, "2026-10-28T00:00:00Z", title='Revert "Add quadrant words"'),
                     _pr(10, "2026-10-31T00:00:00Z", title="Fix main red after 14725")]
        self.assertEqual(lane(q), [10, 9, 8, 4, 3, 2, 1])

    def test_revert_word_mid_title_is_not_a_revert(self):
        q = [_pr(1, "2026-10-30T00:00:00Z", title="Add an undo and revert button to HQ"),
             _pr(2, "2026-10-31T00:00:00Z")]
        self.assertEqual(lane(q, label=""), [2, 1])

    def test_a_draft_still_waits_unless_it_is_the_explicit_pr(self):
        self.assertNotIn(5, lane(QUEUE))
        self.assertIn(5, lane(QUEUE, explicit="5"))

    def test_a_pr_with_no_labels_field_does_not_break_the_sort(self):
        self.assertEqual(lane([{"number": 7, "createdAt": "x", "isDraft": False}]), [7])

    def test_pick_pr_asks_gh_for_labels(self):
        self.assertIn("--json number,isDraft,createdAt,labels,title", PICK_SRC)
        self.assertIn('PRIORITY_LABEL="${JUDGE_JUDY_PRIORITY_LABEL-review:next}"', SH)


if __name__ == "__main__":
    unittest.main()
