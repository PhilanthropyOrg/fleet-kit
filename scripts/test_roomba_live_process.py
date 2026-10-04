"""gh#8357: roomba keeps a worktree a live process is running inside, even if the launcher PID is dead."""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import roomba

with tempfile.TemporaryDirectory() as d:
    assert not roomba.has_live_process_in(d)
    p = subprocess.Popen(["sleep", "30"], cwd=d)
    try:
        assert roomba.has_live_process_in(d)
        sub = os.path.join(d, "x"); os.mkdir(sub)
        assert roomba.has_live_process_in(d)
    finally:
        p.kill(); p.wait()
    assert not roomba.has_live_process_in(d)
assert roomba.has_live_process_in("/tmp", proc_root="/nonexistent")
print("ok")
