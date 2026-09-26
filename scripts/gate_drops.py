#!/usr/bin/env python3
"""gate_drops -- gru's two eligibility gates, with every drop made loud (philanthropy#8215).

THE GAP THIS CLOSES. Reif, 2026-09-26: "so gru passes on something with no quality label and
then never alerts anyone?" vision_link_gate.py and quality_gate.py name their drops, but the
names only ever reached gru.log: 26 of 30 fleet:priority-high items were dropped that day and
nobody was told. This runs both gates in their fixed order (Vision-link, then quality) and,
for each drop:

  * mechanical gap -> fixed here. A `fleet:reif-priority` / `fleet:reif-asked` item with no
    `quality:` label gets `quality:solid` (Reif asked for it; solid is the default bar) and is
    re-gated at once, so it can still be built THIS pass.
  * spec gap (no Vision-link, no quality label on an ordinary item, no Given/When/Then, two
    quality labels) -> `fleet:needs-spec` + ONE comment naming the missing piece (idempotent:
    the same gap is never re-commented) + a single ask per pass for the next brief, listing
    only the items newly labeled this pass.
  * by-design drop (a `fleet:epic` tracking parent, a world-class item waiting on its VP
    design review) -> recorded, nothing else: the process already owns it.

Every drop is appended to $FLEET_LOG_DIR/gate_drops.jsonl; the console's "Gate drops" tile
(`count`) reads it. An item that carries `fleet:needs-spec` and now clears both gates has the
label removed, so it re-enters normally.

The two gates stay the pure cores; `plan()` is pure too (unit-tested, no gh), `apply()` is the
thin exec seam -- same split as dead_end_label.py.

Usage:
  gate_drops.py run --items /tmp/gru_items.json [--run-id ID] [--repo O/R] [--dry-run]
  gate_drops.py count [--hours 6]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quality_gate  # noqa: E402
import vision_link_gate  # noqa: E402
from items_arg import HELP as ITEMS_HELP, load_items  # noqa: E402

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
NEEDS_SPEC = f"{PREFIX}needs-spec"
REIF_LABELS = {f"{PREFIX}reif-priority", f"{PREFIX}reif-asked"}
DEFAULT_QUALITY = "quality:solid"
MARKER = "needs-spec:"
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
DROP_LOG = LOG_DIR / "gate_drops.jsonl"
# philanthropy#8215 amendment: more than this many needs-spec drops in one pass messages
# marie (who fixes specs) and jefe (who watches why they keep arriving unspecced).
MSG_THRESHOLD = int(os.environ.get("FLEET_GATE_DROP_MSG_THRESHOLD", "3"))
MSG_TO = ("marie", "jefe")

# What each gate's drop reason means for the person who has to fix it.
GAPS = (
    ("no Vision-link", "vision-link",
     "a `Vision-link:` line naming a KR id from scripts/okr.json, or `Vision-link: none (maintenance)`"),
    ("no quality: label", "quality-label",
     "exactly one `quality:ship-it` / `quality:solid` / `quality:world-class` label"),
    ("more than one quality label", "quality-label",
     "exactly one `quality:` label (it has several)"),
    ("no Given/When/Then", "acceptance",
     "at least one Given/When/Then acceptance criterion in the body or the newest PRD comment"),
)
BY_DESIGN = ("tracking-only parent", "world-class with no")


def _names(labels) -> list[str]:
    return [lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in labels or []]


def gap_for(reason: str) -> tuple[str, str] | None:
    """(key, what's missing) for a spec gap, None for a by-design drop."""
    if reason.startswith(BY_DESIGN):
        return None
    for prefix, key, missing in GAPS:
        if reason.startswith(prefix):
            return key, missing
    return "other", reason


def comment_body(key: str, missing: str, reason: str, run_id: str) -> str:
    return (f"{MARKER} {key}\n\n"
            f"gru skipped this item (pass {run_id}) because it is missing {missing}.\n\n"
            f"Gate said: {reason}\n\n"
            f"Add the missing piece and the next gru pass picks it up; the `{NEEDS_SPEC}` label "
            f"comes off by itself once it clears the gate.")


def _has_marker(comments, key: str) -> bool:
    line = f"{MARKER} {key}"
    return any((c.get("body") or "").lstrip().startswith(line) for c in comments or [])


def plan(items: list[dict], run_id: str) -> dict:
    """Pure. Decides every label/comment/ask and the final eligible list; runs no gh."""
    by_num = {it["number"]: it for it in items}
    vis = vision_link_gate.gate_candidates(items)
    survivors = [by_num[n] for n in vis["eligible"]]
    qual = quality_gate.gate_candidates(survivors)

    drops = [dict(d, gate="vision-link") for d in vis["dropped"]]
    drops += [dict(d, gate="quality") for d in qual["dropped"]]
    eligible = list(qual["eligible"])
    actions: list[dict] = []   # {"number", "op": add_label|remove_label|comment, ...}
    records: list[dict] = []
    newly: list[dict] = []

    for d in drops:
        it = by_num[d["number"]]
        labels = _names(it.get("labels"))
        rec = {"number": d["number"], "gate": d["gate"], "reason": d["reason"]}
        # Mechanical: a Reif item with no quality label gets the default bar and is re-gated.
        if (d["gate"] == "quality" and d["reason"].startswith("no quality: label")
                and REIF_LABELS & set(labels)):
            actions.append({"number": d["number"], "op": "add_label", "label": DEFAULT_QUALITY})
            labels = labels + [DEFAULT_QUALITY]
            ok, why = quality_gate.classify_candidate(labels, it.get("body"), it.get("comments"))
            if ok:
                eligible.append(d["number"])
                records.append(dict(rec, action="fixed"))
                continue
            rec["reason"] = why  # still short of the bar, for the next reason
        gap = gap_for(rec["reason"])
        if gap is None:
            records.append(dict(rec, action="by-design"))
            continue
        key, missing = gap
        if NEEDS_SPEC not in labels:
            actions.append({"number": d["number"], "op": "add_label", "label": NEEDS_SPEC})
            newly.append({"number": d["number"], "missing": missing})
        if not _has_marker(it.get("comments"), key):
            actions.append({"number": d["number"], "op": "comment",
                            "body": comment_body(key, missing, rec["reason"], run_id)})
        records.append(dict(rec, action="needs-spec", gap=key))

    # Keep marie's order: eligible in the order the items came in.
    order = {it["number"]: i for i, it in enumerate(items)}
    eligible = sorted(set(eligible), key=order.get)
    for n in eligible:
        if NEEDS_SPEC in _names(by_num[n].get("labels")):
            actions.append({"number": n, "op": "remove_label", "label": NEEDS_SPEC})

    ask = None
    if newly:
        listed = "; ".join(f"#{x['number']} needs {x['missing']}" for x in newly)
        ask = {
            "why": (f"gru dropped {len(newly)} item(s) at its build gates this pass and labeled "
                    f"them {NEEDS_SPEC}, each with a comment naming the gap: {listed}"),
            "summary": (f"{len(newly)} backlog item(s) can't be built until someone adds the "
                        f"missing spec"),
            "unblocks": "those items become buildable on the next gru pass",
            "proposed": "marie adds the missing piece in her next pass; reply only to override",
        }
    return {"eligible": eligible, "dropped": records, "actions": actions, "ask": ask,
            "message": message(records, len(items), run_id)}


def message(records: list[dict], candidates: int, run_id: str) -> dict | None:
    """The fleet_msg.py message gru sends marie + jefe when more than MSG_THRESHOLD items
    still need a spec after this pass (Reif's spec amendment on #8215: "shouldn't both jefe
    and marie get a notice?"). Pure; None at or under the threshold."""
    needs = [r for r in records if r.get("action") == "needs-spec"]
    if len(needs) <= MSG_THRESHOLD:
        return None
    by_gap: dict[str, list[int]] = {}
    for r in needs:
        by_gap.setdefault(r.get("gap") or "other", []).append(r["number"])
    lines = [f"gru's build gates dropped {len(needs)} of {candidates} candidates in pass "
             f"{run_id} for a missing spec piece; each carries `{NEEDS_SPEC}` and a "
             f"`{MARKER} <gap>` comment. Fix them so gru can build them:"]
    for gap, nums in sorted(by_gap.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {gap} ({len(nums)}): " + ", ".join(f"#{n}" for n in nums))
    return {"to": MSG_TO, "kind": "gate-drop", "key": "gate-drops",
            "body": "\n".join(lines), "items": [r["number"] for r in needs]}


def _gh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def apply(p: dict, repo: str | None, run_id: str, run=_gh, db_path: str | None = None) -> dict:
    """Exec seam: gh calls, the drop log, and the ask. Best-effort per action, reported."""
    repo_args = ["--repo", repo] if repo else []
    results = []
    for a in p["actions"]:
        if a["op"] == "comment":
            cmd = ["gh", "issue", "comment", str(a["number"]), *repo_args, "--body", a["body"]]
        else:
            flag = "--add-label" if a["op"] == "add_label" else "--remove-label"
            cmd = ["gh", "issue", "edit", str(a["number"]), *repo_args, flag, a["label"]]
        r = run(cmd)
        if r.returncode != 0 and a["op"] == "add_label" and "not found" in (r.stderr or ""):
            run(["gh", "label", "create", a["label"], *repo_args, "--color", "D93F0B",
                 "--description", "gru dropped it at a build gate; the comment names the gap"])
            r = run(cmd)
        results.append(dict(a, ok=r.returncode == 0, **({} if r.returncode == 0 else
                                                        {"error": (r.stderr or "")[:200]})))
    now = time.time()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with DROP_LOG.open("a") as fh:
            for rec in p["dropped"]:
                fh.write(json.dumps(dict(rec, ts=now, run_id=run_id)) + "\n")
    except OSError as exc:
        print(f"gate_drops: could not append {DROP_LOG}: {exc}", file=sys.stderr)
    ask_id = None
    if p["ask"]:
        import ask as ask_mod
        import fleet_db
        conn = fleet_db.connect(Path(db_path) if db_path else None)
        # No page: the brief carries it (messenger_brief.asks_open), one voice to Reif.
        ask_id = ask_mod.file_ask(conn, "gru", p["ask"]["why"], p["ask"]["unblocks"],
                                  p["ask"]["proposed"], ask_class="decision",
                                  summary=p["ask"]["summary"])
    sent = None
    if p.get("message"):
        try:
            import fleet_db
            import fleet_msg
            conn = fleet_db.connect(Path(db_path) if db_path else None)
            m = p["message"]
            sent = fleet_msg.send(conn, "gru", list(m["to"]), m["kind"], m["key"], m["body"],
                                  m["items"])
        except Exception as exc:  # noqa: BLE001 -- the gates' result must still reach gru
            print(f"gate_drops: message not sent: {exc}", file=sys.stderr)
    return {"results": results, "ask_id": ask_id, "sent": sent}


def count(hours: float = 6.0, now: float | None = None, path: Path | None = None) -> dict:
    """Distinct items dropped in the window (an item dropped every hour counts once)."""
    now = now or time.time()
    by_action: dict[str, set] = {}
    try:
        lines = (path or DROP_LOG).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if now - float(rec.get("ts") or 0) <= hours * 3600:
            by_action.setdefault(rec.get("action") or "?", set()).add(rec.get("number"))
    items = set().union(*by_action.values()) if by_action else set()
    return {"items": len(items), **{k: len(v) for k, v in sorted(by_action.items())}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="gate the candidates and make every drop visible")
    r.add_argument("--items", required=True, help="{number, labels, body, comments} list: " + ITEMS_HELP)
    r.add_argument("--run-id", default=os.environ.get("FLEET_RUN_ID") or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    r.add_argument("--repo", default=None)
    r.add_argument("--db-path", default=None)
    r.add_argument("--dry-run", action="store_true", help="print the plan, run no gh, write nothing")
    c = sub.add_parser("count", help="distinct items dropped in the last N hours")
    c.add_argument("--hours", type=float, default=6.0)
    a = ap.parse_args(argv)
    if a.cmd == "count":
        print(json.dumps(count(a.hours)))
        return 0
    p = plan(load_items(a.items), a.run_id)
    if not a.dry_run:
        p.update(apply(p, a.repo, a.run_id, db_path=a.db_path))
    print(json.dumps(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
