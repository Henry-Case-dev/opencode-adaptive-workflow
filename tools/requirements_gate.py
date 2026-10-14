#!/usr/bin/env python3
"""Dependency-free, conservative requirement source inventory and release check.

IMPORTANT: The gate detects missing inventory dispositions and missing evidence;
it does not prove semantic completeness or runtime correctness by itself.

Usage:
  python <OPENCODE_CONFIG>/tools/requirements_gate.py init --source plans/runtime_task.md --map plans/features/X/requirements-map.json
  python <OPENCODE_CONFIG>/tools/requirements_gate.py check --map plans/features/X/requirements-map.json --stage plan
  python <OPENCODE_CONFIG>/tools/requirements_gate.py check --map plans/features/X/requirements-map.json --stage review --slice F1 --candidate <sha>
  python <OPENCODE_CONFIG>/tools/requirements_gate.py check --map plans/features/X/requirements-map.json --stage release --candidate <sha> --deployed-commit <sha>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

INVENTORY_PATTERN = re.compile(r"^\s*(?:#{2,5}\s+|[-*+]\s+|(?:\d+[.)]|[A-Z]\d+[.):])\s+)\S")
REQUIRED = {"required"}
OTHER = {"context", "duplicate", "user_excluded"}
STATES = {"PENDING", "BUILDING", "REVIEW", "VERIFIED", "FAILED", "BLOCKED_REAL_HUMAN_GATE", "READY_FOR_LIVE"}


def sha(s: bytes) -> str:
    return hashlib.sha256(s).hexdigest()


def extract(text: str) -> list[dict]:
    lines = text.splitlines()
    candidates = []
    # Treat substantive Markdown headings/list items as inventory candidates;
    # prose-only constraints MUST be added manually by PM (see README).
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not INVENTORY_PATTERN.match(line) or len(stripped) < 9:
            continue
        candidates.append({
            "source_id": f"L{i:05d}", "source_line": i,
            "source_text_sha256": sha(line.encode("utf-8")),
            "source_excerpt": stripped[:240], "disposition": "unassigned",
            "reason": "", "req_id": "", "slice": "", "acceptance": "",
            "live_required": False, "live_when": "preprod", "status": "PENDING", "evidence": [],
        })
    if not candidates:
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if len(stripped) >= 9:
                candidates.append({
                    "source_id": f"L{i:05d}", "source_line": i,
                    "source_text_sha256": sha(line.encode("utf-8")),
                    "source_excerpt": stripped[:240], "disposition": "unassigned",
                    "reason": "", "req_id": "", "slice": "", "acceptance": "",
                    "live_required": False, "live_when": "preprod", "status": "PENDING", "evidence": [],
                })
    return candidates


def init(args) -> int:
    source = Path(args.source).resolve()
    out = Path(args.map)
    if out.exists() and not args.force:
        print(f"ERROR: map exists; preserve decisions: {out}. Use --force only for explicit rebuild.", file=sys.stderr)
        return 2
    raw = source.read_bytes()
    data = {"schema": "openworkflow-coverage-v1", "source_path": str(source),
            "source_sha256": sha(raw), "candidate_commit": "",
            "deployed_commit": "", "additional_requirements": [],
            "items": extract(raw.decode("utf-8-sig"))}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created {out}: {len(data['items'])} source lines need classification; PM must add prose-only constraints manually.")
    return 0


def check(args) -> int:
    p = Path(args.map)
    data = json.loads(p.read_text(encoding="utf-8"))
    errors: list[str] = []
    if data.get("schema") != "openworkflow-coverage-v1":
        errors.append("unknown map schema")
    sp = Path(data.get("source_path", ""))
    if not sp.is_file():
        errors.append(f"source not present: {sp}")
        lines = []
    else:
        raw = sp.read_bytes()
        if data.get("source_sha256") != sha(raw):
            errors.append("authoritative source changed: reconcile user task and rebuild map (do not overwrite classifications blindly)")
        lines = raw.decode("utf-8-sig").splitlines()
    actual = {v["source_id"]: v for v in extract("\n".join(lines))}
    listed = data.get("items", [])
    if not isinstance(listed, list) or not listed:
        errors.append("no inventory items")
        listed = []
    identities = set()
    for row in listed:
        sid = row.get("source_id", "")
        if sid in identities:
            errors.append(f"duplicate source_id {sid}")
        identities.add(sid)
        if sid not in actual:
            errors.append(f"inventoried source item gone or edited {sid}")
        elif row.get("source_text_sha256") != actual[sid]["source_text_sha256"]:
            errors.append(f"source item hash mismatch {sid}")
    for sid in actual.keys() - identities:
        errors.append(f"source item not mapped {sid}")
    reqids = set()
    scoped_required = []
    manual = data.get("additional_requirements", [])
    if not isinstance(manual, list):
        errors.append("additional_requirements must be array")
        manual = []
    for row in listed + manual:
        sid = row.get("source_id", "MANUAL")
        d = row.get("disposition", "unassigned")
        if d not in REQUIRED | OTHER:
            errors.append(f"{sid}: disposition must be classified, got {d}")
            continue
        if d in OTHER:
            if len(str(row.get("reason", "")).strip()) < 8:
                errors.append(f"{sid}: {d} requires specific reason")
            if d == "user_excluded" and not str(row.get("user_approval_ref", "")).strip():
                errors.append(f"{sid}: user exclusion needs explicit approval reference")
            continue
        rid = str(row.get("req_id", "")).strip()
        if not rid or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,60}", rid):
            errors.append(f"{sid}: required item needs stable req_id")
        # Multiple source clauses can feed one requirement, but their owners must be traceable.
        if not str(row.get("slice", "")).strip():
            errors.append(f"{sid}: required item needs owning slice")
        if len(str(row.get("acceptance", "")).strip()) < 12:
            errors.append(f"{sid}: required item needs observable acceptance")
        if not isinstance(row.get("live_required"), bool):
            errors.append(f"{sid}: live_required must be bool")
        if row.get("live_when", "preprod") not in ("preprod", "postdeploy"):
            errors.append(f"{sid}: live_when must be preprod or postdeploy")
        if row.get("status") not in STATES:
            errors.append(f"{sid}: invalid status {row.get('status')}")
        reqids.add(rid)
        if args.stage == "release" or (args.stage == "review" and row.get("slice") == args.slice):
            scoped_required.append(row)
    if args.stage == "review" and not args.slice:
        errors.append("review stage requires --slice; release must check ALL slices")
    if args.stage == "review" and not scoped_required:
        errors.append(f"review scope has no requirements: {args.slice}")
    if args.stage in {"review", "release"}:
        expected = data.get("candidate_commit")
        if not args.candidate or not expected or args.candidate != expected:
            errors.append("candidate mismatch; map candidate_commit must match --candidate")
        for row in scoped_required:
            rid = row.get("req_id", row.get("source_id", "?"))
            postdeploy = bool(row.get("live_required")) and row.get("live_when") == "postdeploy"
            allowed = {"VERIFIED"}
            if args.stage == "review" and postdeploy:
                allowed.add("READY_FOR_LIVE")
            if row.get("status") not in allowed:
                errors.append(f"{rid}: mandatory not {sorted(allowed)} (got {row.get('status')})")
                continue
            ev = row.get("evidence", [])
            if not isinstance(ev, list) or not ev:
                errors.append(f"{rid}: evidence missing")
                continue
            bound = [x for x in ev if isinstance(x, dict) and x.get("candidate_commit") == args.candidate
                     and str(x.get("ref", "")).strip() and str(x.get("observation", "")).strip()]
            if not bound:
                errors.append(f"{rid}: no evidence bound to reviewed candidate")
            if row.get("live_required") and (args.stage == "release" or row.get("live_when", "preprod") == "preprod"):
                acceptable = ("live", "ui", "provider") if args.stage == "review" else ("live", "production")
                if not any(x.get("type") in acceptable for x in bound):
                    errors.append(f"{rid}: live requirement lacks evidence for stage {args.stage}")
    if args.stage == "release":
        if not args.deployed_commit or args.deployed_commit != args.candidate:
            errors.append("deployed commit missing or differs from reviewed candidate")
        if data.get("deployed_commit") != args.deployed_commit:
            errors.append("map deployed_commit does not match actual deployment")
    for e in errors[:50]:
        print("FAIL:", e)
    if len(errors) > 50:
        print(f"... and {len(errors)-50} more")
    print(f"stage={args.stage} inventory={len(listed)} manual={len(manual)} scoped={len(scoped_required)} errors={len(errors)}")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init")
    i.add_argument("--source", required=True)
    i.add_argument("--map", required=True)
    i.add_argument("--force", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--map", required=True)
    c.add_argument("--stage", choices=("plan", "review", "release"), required=True)
    c.add_argument("--slice")
    c.add_argument("--candidate")
    c.add_argument("--deployed-commit")
    a = ap.parse_args()
    return init(a) if a.command == "init" else check(a)


if __name__ == "__main__":
    sys.exit(main())
