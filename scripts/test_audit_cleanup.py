#!/usr/bin/env python3
"""fk9: an auditor pass resets every QA persona it used and confirms its fixture job is
closed, and audit_forward finds the repo slug the same way audit_file does. RED without the
fix: audit_cleanup.py does not exist and audit_forward ignores repo_slug.
Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_cleanup as ac  # noqa: E402
import audit_file as af  # noqa: E402
import audit_forward as fwd  # noqa: E402


def _fake(codes):
    calls = []

    def http(method, url, headers, body=None):
        calls.append((method, url, headers, json.loads(body) if body else None))
        return codes(method, url, body)
    return http, calls


def test_run_resets_each_persona_in_the_state_file_and_clears_it():
    with tempfile.TemporaryDirectory() as d:
        st = str(Path(d) / "audit-pass.json")
        for p in ("operator", "owner", "operator"):
            ac.main(["persona", p, "--state", st])
        ac.main(["job", "https://philanthropy.org/990/jobs/123", "--state", st])
        http, calls = _fake(lambda m, u, b: 200 if m == "POST" else 410)
        os.environ.update(QA_SESSION_TOKEN="t", ATLAS_TEST_BYPASS="b")
        assert ac.main(["run", "--state", st], http=http) == 0
        resets = [c for c in calls if c[0] == "POST"]
        assert [c[3] for c in resets] == [{"user": "operator"}, {"user": "owner"}], resets
        assert all(c[1].endswith("/990/api/qa/reset") and c[2]["x-qa-token"] == "t"
                   and c[2]["x-atlas-test"] == "b" for c in resets)
        assert [c[1] for c in calls if c[0] == "GET"] == ["https://philanthropy.org/990/jobs/123"]
        assert not Path(st).exists()


def test_a_failed_reset_or_open_job_exits_1_and_keeps_the_state():
    with tempfile.TemporaryDirectory() as d:
        st = str(Path(d) / "audit-pass.json")
        ac.main(["persona", "operator", "--state", st])
        ac.main(["job", "https://philanthropy.org/990/jobs/9", "--state", st])
        http, _ = _fake(lambda m, u, b: 500 if m == "POST" else 200)
        os.environ["QA_SESSION_TOKEN"] = "t"
        assert ac.main(["run", "--state", st], http=http) == 1
        assert Path(st).exists()
        lines = ac.cleanup(ac.load(Path(st)), {}, http=http)
        assert lines[0].startswith("LEFT persona operator: QA_SESSION_TOKEN is unset"), lines
        assert lines[1].startswith("LEFT job"), lines


def test_charter_runs_cleanup_at_pass_end():
    md = (Path(__file__).resolve().parent.parent / "members/auditor/auditor.md").read_text()
    assert "audit_cleanup.py run" in md and "audit_cleanup.py persona" in md


def test_forward_repo_defaults_through_repo_slug_never_fleet_repo():
    seen = []
    saved = af.repo_slug, fwd.open_audit_issues, fwd.load_sent
    af.repo_slug = lambda r="": seen.append(r) or "PhilanthropyOrg/philanthropy"
    fwd.open_audit_issues = lambda repo: seen.append(repo) or []
    fwd.load_sent = lambda: set()
    os.environ["FLEET_REPO"] = "/repo"
    try:
        assert fwd.main(["pending"]) == 0
    finally:
        af.repo_slug, fwd.open_audit_issues, fwd.load_sent = saved
    assert seen == ["", "PhilanthropyOrg/philanthropy"], seen


def test_repo_slug_resolves_a_path_with_gh_and_keeps_a_slug():
    asked = []
    gh = lambda args: asked.append(args) or "PhilanthropyOrg/philanthropy\n"  # noqa: E731
    assert af.repo_slug("/repo", gh=gh) == "PhilanthropyOrg/philanthropy"
    assert af.repo_slug("", gh=gh) == "PhilanthropyOrg/philanthropy"
    assert af.repo_slug("Owner/name", gh=gh) == "Owner/name"
    assert len(asked) == 2 and asked[0][:2] == ["repo", "view"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
