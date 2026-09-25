#!/usr/bin/env python3
"""preflight_gate.py -- the repo's own lint and cheap CI gates, run BEFORE the push.

THE GAP (2026-09-25). Minion PRs kept landing red on checks a builder can run in seconds:
ruff I001 (unsorted imports) in src/philanthropy/api/app.py whenever a PR added a router, the
same on a new test file (#7986), `ruff format --check` (#7982), the repo-health ratchet
(#7975, #7986: a "giant" file grew), docs freshness (#7982). verified_test.sh ran only the
diff-scoped tests, and the container had no ruff at all, so none of it could be caught locally.
The PR was pushed, CI went red, and the builder had already left (see pr_ci_wait.py).

What this runs, in the worktree, on what the branch changed:

  1. ruff check --fix  <changed .py files>   autofix first: I001 and friends are free to fix
  2. ruff format       <changed .py files>
  3. ruff check        <changed .py files>   what is still wrong after the autofix blocks
  4. ruff format --check <changed .py files>
  5. every repo gate script in FLEET_PREFLIGHT_CHECKS that exists in this repo (defaults below,
     the cheap script gates philanthropy's CI `test` job runs before its suite; a repo that
     ships none of them skips this step)

Scoped to the changed files on purpose: a main that is already dirty is a broken gate for
every PR (minion.md's systemic-failure rule), not this branch's defect to fix.

ruff comes from, in order: $FLEET_RUFF, the image's /opt/fleet-ruff/<ver>, a cached
pip --target install of <ver> under $FLEET_RUFF_CACHE, then `ruff` on PATH. <ver> is the pin
the repo's own CI installs (`ruff==X` in .github/workflows/*.yml), so local and CI agree. If
no ruff can be had at all, the lint half is SKIPPED LOUDLY (never silently passed) and the
rest still runs -- a pip outage must not stop every push in the fleet.

Exit 0 = clean (files may have been rewritten by the autofix), 1 = something still fails.
verified_test.sh calls this before the tests, so the receipt the push hook checks covers lint.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CHECKS = (
    "python3 scripts/check_docs.py",
    "python3 scripts/docs_freshness.py --diff origin/main",
    "python3 scripts/repo_health.py --check",
    "python3 scripts/check_template_style_comments.py",
    "python3 scripts/check_no_cruft_files.py",
    "python3 scripts/check_test_shell_antipatterns.py",
)
RUFF_PIN_RE = re.compile(r"ruff==([0-9][0-9A-Za-z.\-]*)")


def _git(args: list[str], cwd: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    return p.stdout if p.returncode == 0 else ""


def uses_ruff(wt: str) -> bool:
    root = Path(wt)
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return True
    py = root / "pyproject.toml"
    return py.exists() and "[tool.ruff" in py.read_text(errors="replace")


def ruff_pin(wt: str) -> str | None:
    """The ruff version the repo's CI installs, so a local pass lints exactly like CI does."""
    wf = Path(wt) / ".github" / "workflows"
    if not wf.is_dir():
        return None
    for f in sorted(wf.glob("*.y*ml")):
        m = RUFF_PIN_RE.search(f.read_text(errors="replace"))
        if m:
            return m.group(1)
    return None


def _version_of(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip().split()[-1] if out.returncode == 0 and out.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def find_ruff(pin: str | None, env: dict, install=True) -> tuple[str | None, str]:
    """(path to a ruff binary or None, one line saying where it came from)."""
    if env.get("FLEET_RUFF"):
        return env["FLEET_RUFF"], "FLEET_RUFF"
    candidates = []
    if pin:
        candidates.append(Path(env.get("FLEET_RUFF_IMAGE_DIR", "/opt/fleet-ruff")) / pin / "bin" / "ruff")
        cache = Path(env.get("FLEET_RUFF_CACHE", os.path.join(env.get("TMPDIR", "/tmp"), "fleet-ruff")))
        candidates.append(cache / pin / "bin" / "ruff")
        for c in candidates:
            if c.exists():
                return str(c), f"ruff {pin} at {c}"
        if install:
            target = cache / pin
            p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target",
                                str(target), f"ruff=={pin}"], capture_output=True, text=True,
                               timeout=300)
            if p.returncode == 0 and (target / "bin" / "ruff").exists():
                return str(target / "bin" / "ruff"), f"ruff {pin} installed to {target}"
    on_path = shutil.which("ruff")
    if on_path:
        v = _version_of(on_path)
        note = f"ruff {v} on PATH" + (f" (CI pins {pin} -- results may differ)" if pin and v != pin else "")
        return on_path, note
    return None, "no ruff available"


def changed_py_files(wt: str, base: str) -> list[str]:
    merge_base = _git(["merge-base", "HEAD", base], wt).strip()
    names: list[str] = []
    if merge_base:
        names += _git(["diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}", "--"], wt).splitlines()
    names += _git(["diff", "--name-only", "--diff-filter=ACMR", "HEAD", "--"], wt).splitlines()
    names += _git(["ls-files", "--others", "--exclude-standard"], wt).splitlines()
    seen, out = set(), []
    for n in names:
        n = n.strip()
        if n and n.endswith(".py") and n not in seen and (Path(wt) / n).is_file():
            seen.add(n)
            out.append(n)
    return out


def _run(cmd: list[str], cwd: str) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def repo_checks(wt: str, env: dict) -> list[str]:
    raw = env.get("FLEET_PREFLIGHT_CHECKS")
    cmds = [c.strip() for c in raw.split(";")] if raw is not None else list(DEFAULT_CHECKS)
    keep = []
    for c in cmds:
        if not c:
            continue
        toks = shlex.split(c)
        script = next((t for t in toks[1:] if t.endswith(".py") or t.endswith(".sh")), None)
        if script and not (Path(wt) / script).is_file():
            continue  # this repo does not ship that gate
        keep.append(c)
    return keep


def run(wt: str, env: dict | None = None, out=print) -> int:
    env = dict(os.environ if env is None else env)
    base = env.get("FLEET_PREFLIGHT_BASE", "origin/main")
    failed: list[str] = []

    if uses_ruff(wt):
        files = changed_py_files(wt, base)
        if files:
            ruff, where = find_ruff(ruff_pin(wt), env)
            if not ruff:
                out(f"preflight: LINT SKIPPED -- {where}; CI's lint job will be the first to see "
                    f"these {len(files)} file(s). Install ruff in the image.")
            else:
                out(f"preflight: {where}; {len(files)} changed .py file(s)")
                _run([ruff, "check", "--fix", "--quiet", *files], wt)
                _run([ruff, "format", "--quiet", *files], wt)
                rc, text = _run([ruff, "check", *files], wt)
                if rc != 0:
                    failed.append("ruff check")
                    out(f"preflight: ruff check still failing after --fix:\n{text}")
                rc, text = _run([ruff, "format", "--check", *files], wt)
                if rc != 0:
                    failed.append("ruff format")
                    out(f"preflight: ruff format --check failing:\n{text}")
        else:
            out("preflight: no changed .py files -- lint has nothing to check")

    for cmd in repo_checks(wt, env):
        rc, text = _run(shlex.split(cmd), wt)
        if rc != 0:
            failed.append(cmd)
            out(f"preflight: FAILED `{cmd}` (exit {rc}):\n{text[-3000:]}")
        else:
            out(f"preflight: ok `{cmd}`")

    if failed:
        out(f"preflight: {len(failed)} gate(s) red -- fix before pushing: {', '.join(failed)}")
        return 1
    out("preflight: clean")
    return 0


def main(argv: list[str]) -> int:
    wt = argv[0] if argv else (os.environ.get("WT_PATH") or os.getcwd())
    return run(wt)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
