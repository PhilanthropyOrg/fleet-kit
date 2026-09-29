#!/usr/bin/env python3
"""log_rotate.py -- copytruncate every <member>.log and access.jsonl past a size cap.

    python3 log_rotate.py <log_dir> [--max-mb 50]

Nothing rotated these: on dino (2026-09-28) access.jsonl was 205MB and minion.log 107MB after
five weeks, growing with minion throughput. A file past the cap is gzipped to <name>.1.gz
(replacing the previous generation) and then truncated IN PLACE -- the logrotate
`copytruncate` shape. In place, not rename: every writer appends with O_APPEND (`>>`, open 'a')
on a long-lived fd, and every live tailer (fleet_view_server's log feed, auto_deploy_race_check)
already treats "file got smaller" as rotated and rereads from 0. A rename would leave writers
appending to the renamed inode. The .1.gz name is outside the `*.log` glob those tailers walk.

State-bearing jsonl files (runs.jsonl -- fleet.db syncs it by offset --, inbox.jsonl, leases,
...) are never touched: only *.log and access.jsonl.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
from pathlib import Path

ROTATE_EXTRA = ("access.jsonl",)


def rotate(log_dir: Path, max_bytes: int) -> list[str]:
    done = []
    for p in sorted(list(log_dir.glob("*.log")) + [log_dir / n for n in ROTATE_EXTRA]):
        try:
            if not p.is_file() or p.stat().st_size <= max_bytes:
                continue
            gz = p.with_name(p.name + ".1.gz")
            tmp = gz.with_name(gz.name + ".tmp")
            with open(p, "rb") as src, gzip.open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
                copied = src.tell()
            os.replace(tmp, gz)
            # Keep whatever was appended while we were copying: truncate to 0 only if nothing
            # new landed, else move the new tail to the front.
            with open(p, "r+b") as fh:
                fh.seek(copied)
                tail = fh.read()
                fh.seek(0)
                fh.write(tail)
                fh.truncate(len(tail))
            done.append(f"{p.name}: {copied // 1_000_000}MB -> {gz.name}")
        except OSError as exc:
            print(f"log_rotate: {p}: {exc}", file=sys.stderr)
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_dir")
    ap.add_argument("--max-mb", type=float, default=float(os.environ.get("FLEET_LOG_ROTATE_MB", "50")))
    a = ap.parse_args()
    for line in rotate(Path(a.log_dir), int(a.max_mb * 1_000_000)):
        print(f"log_rotate: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
