"""gate_drops.list_open retries a transient GitHub failure instead of failing the intake (jefe msg#834)."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_drops  # noqa: E402


def _cp(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def test_a_504_then_success_returns_the_items():
    replies = [_cp(1, err="HTTP 504: 504 Gateway Timeout"), _cp(0, out='[{"number": 1}]')]
    got = gate_drops.list_open("fleet:backlog", None, run=lambda cmd: replies.pop(0), sleep=lambda s: None)
    assert got == [{"number": 1}]


def test_a_cut_off_body_is_retried():
    replies = [_cp(0, out='[{"numb'), _cp(0, out="[]")]
    assert gate_drops.list_open("fleet:backlog", None, run=lambda cmd: replies.pop(0), sleep=lambda s: None) == []


def test_still_failing_after_every_try_raises_with_the_last_error():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return _cp(1, err="unexpected end of JSON input")

    with pytest.raises(RuntimeError, match="unexpected end of JSON input"):
        gate_drops.list_open("fleet:backlog", None, run=run, sleep=lambda s: None)
    assert len(calls) == gate_drops.LIST_OPEN_TRIES
