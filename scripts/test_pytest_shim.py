"""scripts/bin/pytest runs the interpreter test_python.sh names, with the worktree's src first."""
import os
import subprocess
from pathlib import Path

SHIM = Path(__file__).resolve().parent / "bin" / "pytest"


def test_shim_execs_test_python_with_worktree_src(tmp_path):
    (tmp_path / "src").mkdir()
    fake = tmp_path / "fakepy"
    fake.write_text('#!/usr/bin/env bash\necho "args=$* pythonpath=$PYTHONPATH"\n')
    fake.chmod(0o755)
    env = dict(os.environ, WT_PATH=str(tmp_path), FLEET_TEST_PYTHON=str(fake), PYTHONPATH="")
    out = subprocess.run([str(SHIM), "-q", "tests/test_x.py"], env=env, capture_output=True,
                         text=True, check=True).stdout
    assert out.strip() == f"args=-m pytest -q tests/test_x.py pythonpath={tmp_path}/src"


def test_run_member_puts_shim_dir_first_on_path():
    text = (SHIM.parent.parent / "run_member.sh").read_text()
    assert 'export PATH="$KIT_DIR/scripts/bin:$PATH"' in text
