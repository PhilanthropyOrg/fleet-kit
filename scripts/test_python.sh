#!/usr/bin/env bash
# test_python.sh -- print a python that can run THIS repo's tests, building it once if needed.
#
#   PY=$(bash /fleet-kit/scripts/test_python.sh [<worktree>])
#
# THE GAP (2026-09-25). The container's python3 has none of the product's dependencies, and
# /repo/.venv/bin/python is a symlink to a uv interpreter that exists only on the HOST
# (/home/ubuntu/.local/share/uv/...). Every builder rediscovered that and built its own venv:
# #7975's and #7982's dispatched fixers each spent 10+ of their 30 minutes on
# `pip install --user uv; uv venv /tmp/fx-venv; uv pip install -e .[dev]` and timed out with a
# fix committed and never pushed. #7986's fixer, the one that had a venv left over, merged.
#
# One cached venv per dependency set: keyed on a hash of pyproject.toml (plus any
# requirements*.txt / uv.lock), kept under $FLEET_LOG_DIR/.test-venvs (a host bind mount, so it
# survives redeploys), built under a flock so concurrent passes wait for one build instead of
# racing N. Only the DEPENDENCIES are installed; verified_test.sh puts <worktree>/src first on
# PYTHONPATH so tests import the worktree's code, never a stale installed copy.
#
# A repo with no [project] dependencies (fleet-kit itself) just gets python3. Any build failure
# prints python3 and says so on stderr -- a broken build must not stop a push that the old
# path could still make.
set -uo pipefail

WT="${1:-${WT_PATH:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}}"

if [ -n "${FLEET_TEST_PYTHON:-}" ]; then echo "$FLEET_TEST_PYTHON"; exit 0; fi
if [ ! -f "$WT/pyproject.toml" ] || ! grep -q '^\[project\]' "$WT/pyproject.toml"; then
  command -v python3; exit 0
fi

ROOT="${FLEET_TEST_VENV_ROOT:-${FLEET_LOG_DIR:-${TMPDIR:-/tmp}}/.test-venvs}"
mkdir -p "$ROOT" 2>/dev/null || { command -v python3; exit 0; }
KEY=$(cd "$WT" && cat pyproject.toml $(ls requirements*.txt uv.lock 2>/dev/null) | sha256sum | cut -c1-16)
VENV="$ROOT/$KEY"

if [ -f "$VENV/.ready" ] && [ -x "$VENV/bin/python" ]; then echo "$VENV/bin/python"; exit 0; fi

exec 7>"$ROOT/$KEY.lock"
flock -w "${FLEET_TEST_VENV_LOCK_WAIT_S:-900}" 7 || { echo "test_python: another build of $KEY still running -- using python3" >&2; command -v python3; exit 0; }
if [ -f "$VENV/.ready" ] && [ -x "$VENV/bin/python" ]; then echo "$VENV/bin/python"; exit 0; fi

rm -rf "$VENV"
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
EXTRA=""; grep -qE '^dev *= *\[' "$WT/pyproject.toml" && EXTRA="--extra dev"
PYVER="${FLEET_TEST_PYTHON_VERSION:-3.12}"
echo "test_python: building the test env for $WT ($KEY) -- once per dependency change" >&2
ok=1
if [ -n "$UV" ]; then
  export UV_PYTHON_INSTALL_DIR="$ROOT/pythons"
  { "$UV" venv -q --python "$PYVER" "$VENV" || "$UV" venv -q --python python3 "$VENV"; } >&2 \
    && "$UV" pip install -q --python "$VENV/bin/python" -r "$WT/pyproject.toml" $EXTRA pytest pytest-xdist >&2 \
    || ok=0
else
  PYBIN="$(command -v "python$PYVER" || command -v python3.11 || command -v python3)"
  { "$PYBIN" -m venv "$VENV" && "$VENV/bin/pip" install -q "$WT${EXTRA:+[dev]}" pytest pytest-xdist; } >&2 || ok=0
fi
if [ "$ok" -eq 1 ] && "$VENV/bin/python" -c "import pytest" 2>/dev/null; then
  touch "$VENV/.ready"
  echo "$VENV/bin/python"
else
  echo "test_python: build FAILED for $KEY -- falling back to python3; tests needing the repo's deps will fail" >&2
  command -v python3
fi
