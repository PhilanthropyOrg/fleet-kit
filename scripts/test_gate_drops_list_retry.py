"""gate_drops.list_open retries a transient GitHub failure instead of failing the intake (jefe msg#834)."""
import subprocess
import sys
from pathlib import Path

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

    try:
        gate_drops.list_open("fleet:backlog", None, run=run, sleep=lambda s: None)
        raise AssertionError("list_open must raise once every try failed")
    except RuntimeError as exc:
        assert "unexpected end of JSON input" in str(exc), exc
    assert len(calls) == gate_drops.LIST_OPEN_TRIES


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok    {name}")
