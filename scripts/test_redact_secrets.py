#!/usr/bin/env python3
"""fk#1494: a leaked credential cannot ride the run log, and ordinary text is left alone.

  * every credential class is redacted where it is planted -- in a run record at the source
    (run_report.py), in HANDOFF.md / NORTH.md on the way back into a prompt, and in what the
    dashboard serves;
  * the VALUE of any secret-named variable this process holds is redacted wherever it shows up,
    with no pattern needed;
  * NOTHING ELSE CHANGES: every member charter, the shared law, README, RUNBOOK, docs, every
    shipped shell script and a set of run outcomes in the fleet's real voice come back
    byte-identical;
  * there is ONE pattern list -- members/librarian/librarian.py's -- not two.

Secret-shaped fixtures are assembled from pieces so this file holds no secret-shaped literal.
"""
from __future__ import annotations

import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import redact_secrets as R  # noqa: E402

librarian = R.librarian
MIX = "a1B2c3D4"

# class -> a credential of that class's real shape and length
PLANTED = {
    "gho": "gho_" + MIX * 5,
    "ghp": "ghp_" + MIX * 5,
    "ghs": "ghs_" + MIX * 5,
    "ghu": "ghu_" + MIX * 5,
    "ghr": "ghr_" + MIX * 5,
    "github_pat": "github_pat_" + "11ABCDEFG0" + "_" + MIX * 8,
    "sk-ant": "sk-ant-" + "api03-" + MIX * 6,
    "openai_key": "sk-" + "proj-" + MIX * 8,
    "stripe_key": "sk_" + "live_" + MIX * 4,
    "stripe_webhook": "whsec_" + MIX * 4,
    "resend_key": "re_" + "c1tpEyD8" + "_" + "NKFusih9vKVQknRAQfmFcWCv",
    "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
    "slack_token": "xoxb-" + "1234567890123" + "-" + "1234567890123" + "-" + MIX * 3,
    "bearer_token": "Bearer " + "eyJhbGciOiJIUzI1NiJ9." + MIX * 4,
    "private_key": "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow" + MIX * 6 + "\n-----END RSA " + "PRIVATE KEY-----",
    "pgpassword": "PGPASSWORD=" + "hunter2hunter2",
    "postgres_url": "postgres://" + "app:" + "s3cretpw99" + "@db.internal:5432/atlas",
    "secret_env": "FLASK_SECRET=" + "9f8e7d6c5b4a39281706",
}

# Run outcomes in the fleet's real voice (modelled on runs.jsonl): names of variables, commands
# that mention them, issue numbers, paths, hashes. None holds a credential; none may change.
REALISTIC_OUTCOMES = [
    "QUIET — worktree sweep (10 evaluated, 0 removed); nothing to remove",
    "opened PR #8424 closing #8110, #8112, #8119; verified_test.sh green (412 passed)",
    "scrub 318 scanned, 2 redacted, 0 compressed, 0 dropped; classes to ROTATE: GitHub OAuth: 2 occurrence(s) across 1 file(s)",
    "blocked: no GOOGLE_CLIENT_ID in fleet.env; the number lives only in the Search Console UI",
    "wired GH_TOKEN=$(cat /root/.gh_token) into the */10 git pull cron line (entrypoint.sh:270)",
    "the hook now blocks `env`, `printenv GH_TOKEN` and `echo $GH_TOKEN`; `[ -n \"$GH_TOKEN\" ]` still passes",
    "FLEET_API_KEY is unset before spawn (run_member.sh:65); FLEET_MAXX_KEY follows the handle",
    "curl -s -H \"x-qa-token: $QA_SESSION_TOKEN\" -H \"x-atlas-test: $ATLAS_TEST_BYPASS\" returned 200; HQ loads as the QA owner",
    "Authorization: Bearer $FLEET_WEBHOOK_TOKEN_CANARY was rejected with 401 -- token name was wrong in the caller",
    "set FLEET_WEBHOOK_TOKENS=canary:<tok>,ci:<tok> in fleet.env.example; real values stay on the box",
    "merged at 3f2a9c1e7b6d5a4f3e2d1c0b9a8f7e6d5c4b3a2f; deploy sha matches /healthz",
    "re_validate1 and re_compile_cache renamed; sk-something placeholder removed from the docs",
    "task-1234567890123456789012345678901234567890abcdef finished; risk-proj-review moved to next week",
    "pk_live_ publishable key belongs in the template; the secret key stays in the box's env",
    "rework rate 0.31 -> 0.18 over 24h (fleet_metrics.py rework_rate); 37 merged, 4 pulled",
    "judge-judy BLOCK on #1494: `set -euo pipefail; gh pr list` was refused by the guard",
    "ran `compgen -e | grep -c FLEET_` -> 41; `printenv FLEET_REPO` -> /repo",
    "Evidence: https://github.com/PhilanthropyOrg/fleet-kit/pull/1494#issuecomment-2391847561",
    "the bearer of this ticket gets one retry; password reset flow untouched (routes/auth.py:212)",
    "PGPASSWORD is read from the box's env by scripts/box/db_probe.sh; nothing echoes it",
    "postgres 16.4 on the box; connection string comes from DATABASE_URL (not printed)",
    "self_critique: I assumed PRIMARY_KEY was unique per tenant; it is per table",
    "closed 12 as cruft, 3 folded into PR #8401; SORT_KEY=createdAt for the queue view",
    "lesson: a private key block in a fixture must be built from pieces, or push protection stops the PR",
    "account_status.sh: token: present (len=108); quota: no gate -- pool will try this account",
    "KEY=value pairs in the crontab are world-readable; TOKEN_FILE=/root/.gh_token is 0600",
    "wrote /tmp/link.json ({\"url\": \".../990/auth/magic?token=...\", \"ein\", \"claim_id\"})",
    "AKIA prefix check added to the linter; ASIA temporary keys are out of scope",
    "xoxb- style tokens are not used by this fleet; Slack is not wired",
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 matches the release asset",
    "GA4_SA_KEY=/root/keys/ga4.json exists and is readable; GSC_PROPERTY=sc-domain:example.org",
    "Outcome: sent wrap / Evidence: Resend id 4ef2a1b0-77c1-4f0e-9d3a-2b1c0d9e8f7a",
]


class PlantedSecretsAreRedacted(unittest.TestCase):
    def test_every_class_is_redacted_and_marked(self):
        for cls, secret in PLANTED.items():
            text = f"the deploy log shows {secret} near the end"
            out = R.redact_text(text, env={})
            core = secret.split(" ", 1)[-1] if cls == "bearer_token" else secret
            if cls == "secret_env":
                core = secret.split("=", 1)[1]
            self.assertNotIn(core, out, cls)
            self.assertIn(f"[REDACTED:{cls}]", out, cls)

    def test_secrets_after_a_json_escaped_newline_are_caught(self):
        for cls in ("ghp", "openai_key", "resend_key"):
            out = R.redact_text("2>&1\\n" + PLANTED[cls], env={})
            self.assertIn(f"[REDACTED:{cls}]", out, cls)

    def test_ordinary_identifiers_that_share_a_prefix_survive(self):
        for text in ("re_validate1", "re_compile_pattern_cache_for_everything", "sk-something",
                     "sk-proj-review", "task-" + MIX * 6, "pk_live_" + MIX * 4, "whsec_...",
                     "xoxb-your-token-here", "Bearer $FLEET_WEBHOOK_TOKEN_CI", "Bearer <token>",
                     "github_pat_example", "AKIA", "-----BEGIN PUBLIC KEY-----"):
            self.assertEqual(R.redact_text(text, env={}), text)

    def test_a_row_stays_valid_json_and_is_redacted_at_any_depth(self):
        row = {"member": "minion", "outcome": f"pushed with {PLANTED['ghp']}", "tokens": {"num_turns": 3},
               "items": [1, 2], "nested": {"evidence": [f"log: {PLANTED['pgpassword']} psql"]}}
        out = R.redact_record(row, env={})
        dumped = json.dumps(out)
        self.assertEqual(json.loads(dumped)["tokens"], {"num_turns": 3})
        self.assertEqual(out["items"], [1, 2])
        for secret in (PLANTED["ghp"], "hunter2hunter2"):
            self.assertNotIn(secret, dumped)
        self.assertEqual(row["outcome"], f"pushed with {PLANTED['ghp']}", "the input row is not mutated")


class EnvValuesAreRedacted(unittest.TestCase):
    ENV = {"FLEET_NUMBER_TOKEN": "zq-plain-looking-value-77", "ACME_API_KEY": "nothing-like-a-known-prefix",
           "DB_PASSWORD": "correct horse battery staple", "WEBHOOK_SECRET": "short",
           "GSC_SA_KEY": "/root/keys/gsc-service-account.json", "FLEET_REPO": "/repo-with-a-long-name",
           "TOKEN_HOLDER_NAME": "not-a-secret-name-at-all"}

    def test_the_value_is_replaced_wherever_it_appears(self):
        text = ("curl failed: zq-plain-looking-value-77 rejected; retried with "
                "nothing-like-a-known-prefix; db said 'correct horse battery staple' is wrong")
        out = R.redact_text(text, env=self.ENV)
        for value in ("zq-plain-looking-value-77", "nothing-like-a-known-prefix", "correct horse battery staple"):
            self.assertNotIn(value, out)
        for name in ("FLEET_NUMBER_TOKEN", "ACME_API_KEY", "DB_PASSWORD"):
            self.assertIn(f"[REDACTED:env:{name}]", out)

    def test_short_values_paths_and_ordinary_names_are_left_alone(self):
        text = "short /root/keys/gsc-service-account.json /repo-with-a-long-name not-a-secret-name-at-all"
        self.assertEqual(R.redact_text(text, env=self.ENV), text)

    def test_this_process_own_environment_is_what_run_report_uses(self):
        code = ("import sys; sys.path.insert(0, %r); import run_report\n"
                "rec = run_report.build_record(member='minion', run_id='r1', kind='llm', exit_code=0,\n"
                "    pass_text='Outcome: pushed; the remote was https://x:hush-hush-value-4242@github.com/o/r\\n"
                "Evidence: token %s in the log', usage=None, vision_required=False)\n"
                "import json; print(json.dumps(rec))" % (str(HERE), PLANTED["ghp"]))
        env = dict(os.environ, GH_TOKEN="hush-hush-value-4242")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("hush-hush-value-4242", out.stdout)
        self.assertNotIn(PLANTED["ghp"], out.stdout)
        rec = json.loads(out.stdout)
        self.assertIn("[REDACTED:env:GH_TOKEN]", rec["outcome"])
        self.assertIn("[REDACTED:ghp]", rec["evidence"])


class NothingElseChanges(unittest.TestCase):
    def _files(self):
        pats = ["members/*/*.md", "agents/*.md", "docs/**/*.md", "scripts/*.sh", "members/*/*.sh"]
        files = [p for pat in pats for p in glob.glob(str(ROOT / pat), recursive=True)]
        return sorted(files + [str(ROOT / n) for n in ("README.md", "RUNBOOK.md", "entrypoint.sh", "fleet.env.example")])

    def test_charters_law_readme_docs_and_scripts_come_back_identical(self):
        files = self._files()
        self.assertGreater(len(files), 80)
        changed = []
        for path in files:
            text = Path(path).read_text(errors="replace")
            out = R.redact_text(text, env={})
            if out != text:
                bad = [b for a, b in zip(text.splitlines(), out.splitlines()) if a != b][:2]
                changed.append((os.path.relpath(path, ROOT), bad))
        self.assertEqual(changed, [])

    def test_real_voice_outcomes_come_back_identical(self):
        self.assertGreaterEqual(len(REALISTIC_OUTCOMES), 30)
        changed = [(t, R.redact_text(t, env={})) for t in REALISTIC_OUTCOMES if R.redact_text(t, env={}) != t]
        self.assertEqual(changed, [])

    def test_only_the_planted_secret_changes_inside_real_text(self):
        charter = (ROOT / "members" / "minion" / "minion.md").read_text()
        cut = len(charter) // 2
        out = R.redact_text(charter[:cut] + " " + PLANTED["ghs"] + " " + charter[cut:], env={})
        self.assertEqual(out, charter[:cut] + " [REDACTED:ghs] " + charter[cut:])


class OneList(unittest.TestCase):
    def test_the_redactor_runs_the_scrubbers_own_list(self):
        self.assertIs(R.PATTERNS, librarian.PATTERNS)
        self.assertNotIn("re.compile", (HERE / "redact_secrets.py").read_text().split('"""', 2)[2]
                         .replace("_SECRET_NAME_RE = re.compile", "").replace("_LITERAL_VALUE_RE = re.compile", "")
                         .replace("_ANY_HINT_RE = re.compile", ""),
                         "redact_secrets.py must not grow a pattern list of its own")

    def test_the_transcript_scrub_catches_every_class_too(self):
        for cls, secret in PLANTED.items():
            stats = librarian.ScrubStats()
            out = librarian.redact_text(f"x {secret} y", stats, "t.jsonl")
            self.assertIn(f"[REDACTED:{cls}]", out, cls)
            self.assertIn(cls, librarian.CLASS_LABELS, f"{cls} needs a rollup label for the scrub report")

    def test_a_hint_never_hides_a_real_match(self):
        # could_match() skips a class when its literal is absent; that is only safe if every
        # match of the class contains one of its literals.
        self.assertEqual(sorted(librarian.HINTS), sorted(c for c, _p, _s in librarian.PATTERNS))
        for cls, pattern, _sub in librarian.PATTERNS:
            m = pattern.search(f"x {PLANTED[cls]} y")
            self.assertTrue(m, cls)
            self.assertTrue(librarian.could_match(cls, m.group(0)), cls)


class OnTheWayBack(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        rows = [{"member": "minion", "run_id": f"minion-{i}", "kind": "llm", "ts": time.time() - 60 * i,
                 "status": "ok", "outcome": f"opened PR #{8400 + i}", "evidence": "verified_test.sh green",
                 "self_critique": "none", "lesson": "none", "broken": ""} for i in range(1, 4)]
        # an OLD row, written before run_report.py redacted at the source
        rows.append({"member": "the-fixer", "run_id": "the-fixer-9", "kind": "llm", "ts": time.time() - 30,
                     "status": "ok", "outcome": f"re-ran CI; push used {PLANTED['ghp']}",
                     "evidence": f"remote: {PLANTED['postgres_url']}", "self_critique": "printed the token",
                     "lesson": f"never echo {PLANTED['gho']}", "broken": f"leak {PLANTED['ghs']}"})
        (self.tmp / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.env = dict(os.environ, FLEET_LOG_DIR=str(self.tmp), FLEET_REPO=str(self.tmp))

    def test_handoff_md_never_carries_a_credential_into_the_next_prompt(self):
        p = subprocess.run([sys.executable, str(HERE / "handoff.py"), "write", "--no-gh"],
                           capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        text = (self.tmp / "HANDOFF.md").read_text()
        self.assertIn("the-fixer", text)
        for cls in ("ghp", "gho", "ghs", "postgres_url"):
            self.assertNotIn(PLANTED[cls], text, cls)
        self.assertIn("[REDACTED:", text)

    def test_the_dashboard_serves_no_credential(self):
        os.environ.update(FLEET_LOG_DIR=str(self.tmp), FLEET_REPO=str(self.tmp))
        import fleet_view_server as fvs

        h = fvs.Handler.__new__(fvs.Handler)   # the real _json, with the socket parts stubbed
        h.wfile, h.headers, h.command, h.path = io.BytesIO(), {}, "POST", "/api/runs"
        h.send_response = h.send_header = h.end_headers = h._access_log = lambda *_a, **_k: None
        rows = [json.loads(line) for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        fvs.Handler._json(h, {"runs": rows})
        body = h.wfile.getvalue().decode()
        self.assertEqual(len(json.loads(body)["runs"]), 4)
        self.assertNotIn(PLANTED["ghp"], body)
        self.assertIn("[REDACTED:ghp]", body)

        sent = []
        with fvs._subscribers_lock:
            fvs._subscribers.append(sent)
        try:
            fvs.broadcast("run", rows[-1])
        finally:
            with fvs._subscribers_lock:
                fvs._subscribers.remove(sent)
        self.assertNotIn(PLANTED["ghp"], sent[0])
        self.assertIn("[REDACTED:ghp]", sent[0])


class FastEnough(unittest.TestCase):
    def test_a_full_dashboard_feed_is_redacted_in_well_under_a_second(self):
        rows = [{"member": "minion", "run_id": f"m-{i}", "outcome": REALISTIC_OUTCOMES[i % 32],
                 "evidence": REALISTIC_OUTCOMES[(i + 7) % 32], "report": " ".join(REALISTIC_OUTCOMES[:12]),
                 "self_critique": REALISTIC_OUTCOMES[(i + 3) % 32], "tokens": {"num_turns": i}} for i in range(500)]
        t0 = time.time()
        out = R.redact_record({"runs": rows}, env={})
        took = time.time() - t0
        self.assertEqual(out, {"runs": rows})
        self.assertLess(took, 2.0, f"500-row feed took {took:.2f}s")

    def test_a_clean_megabyte_costs_one_scan_not_eighteen(self):
        text = ("opened PR 8424 and closed nine issues; verified green\n" * 20000)   # ~1MB, no hint in it
        t0 = time.time()
        self.assertEqual(R.redact_text(text, env={}), text)
        self.assertLess(time.time() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
