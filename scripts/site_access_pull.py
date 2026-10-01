#!/usr/bin/env python3
"""site_access_pull.py -- keep a full copy of the live site's nginx access log on the fleet box.

Reif 2026-10-01: "this is the key thing - understand what is happening on the site - and finding
patterns and issues, we need the whole thing". Every 5 minutes (entrypoint.sh cron) this asks the
prod probe key for the bytes nginx added since the last pull (`access-log <inode> <offset>`,
scripts/prod_runtime_probe.sh) and appends them to

    $FLEET_LOG_DIR/site_access/access-YYYY-MM-DD.log.gz   (UTC day of the pull)

Each file is a run of gzip members, so `zcat`, `zgrep` and python's gzip read it whole. A pull
lands only when the whole stream arrived: the bytes go to a temp file first, and the saved
offset moves only after they are appended, so a dropped connection is retried, never doubled.
A line cut off at the end of a pull waits in .partial for the next one. Keeps KEEP_DAYS days.

Secrets in links are blanked before anything is saved: sign-in and confirm links carry a live
`?token=` (161 magic-link GETs in one day, 2026-10-01), and a worker reading the copy must not be
able to sign in as the person who clicked it.

  site_access_pull.py            # one pull (the cron form)
"""
import fcntl
import gzip
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))
OUT = LOG_DIR / "site_access"
KEEP_DAYS = int(os.environ.get("FLEET_SITE_ACCESS_KEEP_DAYS", "14"))
HEADER = re.compile(rb"^#fleet-access-log ino=(\d+) end=(\d+)\n$")
SECRET = re.compile(rb"([?&](?:token|access_token|key|api_key|code|sig|signature|password|secret)=)[^\s&\"]+", re.I)


def probe_cmd(ino: int, off: int) -> list[str]:
    key = os.environ.get("FLEET_PROD_PROBE_KEY", "/fleet-kit/.prod_probe_key")
    host = os.environ.get("FLEET_PROD_PROBE_HOST", "")
    if not host or not Path(key).exists():
        sys.exit(f"no probe access: FLEET_PROD_PROBE_HOST={host or '(unset)'} key={key} exists={Path(key).exists()}")
    return ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={LOG_DIR / '.prod_probe_known_hosts'}",
            host, f"access-log {ino} {off}"]


def pull(cmd_for=probe_cmd, out: Path = OUT, now: float | None = None) -> str:
    now = now or time.time()
    out.mkdir(parents=True, exist_ok=True)
    state_f, partial_f, tmp = out / ".state.json", out / ".partial", out / ".incoming.gz"
    st = json.loads(state_f.read_text()) if state_f.exists() else {"ino": 0, "off": 0}
    buf = partial_f.read_bytes() if partial_f.exists() else b""
    proc = subprocess.Popen(cmd_for(st["ino"], st["off"]), stdout=subprocess.PIPE)
    got = lines = 0
    try:
        with gzip.GzipFile(fileobj=proc.stdout) as src, gzip.open(tmp, "wb") as dst:
            m = HEADER.match(src.readline())
            if not m:
                raise ValueError("no #fleet-access-log header")
            while chunk := src.read(1 << 20):
                got += len(chunk)
                buf += chunk
                cut = buf.rfind(b"\n") + 1
                dst.write(SECRET.sub(rb"\1REDACTED", buf[:cut]))
                lines += buf.count(b"\n", 0, cut)
                buf = buf[cut:]
    except (OSError, EOFError, ValueError) as e:
        proc.kill()
        proc.wait()
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"pull failed, nothing saved: {e}")
    rc = proc.wait()
    proc.stdout.close()
    ino, end = int(m.group(1)), int(m.group(2))
    want = end - st["off"] if ino == st["ino"] and st["off"] <= end else None  # same file: exact size known
    if rc or (want is not None and got != want):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"pull incomplete, nothing saved: rc={rc} got={got} want={want}")
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    with open(tmp, "rb") as f, open(out / f"access-{day}.log.gz", "ab") as dst:
        dst.write(f.read())
    tmp.unlink()
    partial_f.write_bytes(buf)
    state_f.write_text(json.dumps({"ino": ino, "off": end}))
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_DAYS * 86400))
    for old in out.glob("access-*.log.gz"):
        if old.name[7:17] < cutoff:
            old.unlink()
    return f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} +{lines} lines ({got} bytes) -> access-{day}.log.gz"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("previous pull still running")
            return 0
        print(pull())
    return 0


if __name__ == "__main__":
    sys.exit(main())
