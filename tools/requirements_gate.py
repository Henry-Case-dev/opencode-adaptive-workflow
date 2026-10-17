#!/usr/bin/env python3
"""Dependency-free, conservative requirement source inventory and release check.

IMPORTANT: The gate detects missing inventory dispositions and missing evidence;
it does not prove semantic completeness or runtime correctness by itself.

Usage:
  python <OPENCODE_CONFIG>/tools/requirements_gate.py init --source plans/runtime_task.md --map plans/features/X/requirements-map.json
  python <OPENCODE_CONFIG>/tools/requirements_gate.py check --map plans/features/X/requirements-map.json --stage plan
  python <OPENCODE_CONFIG>/tools/requirements_gate.py check --map plans/features/X/requirements-map.json --stage review --slice F1 --candidate <sha>
  python <OPENCODE_CONFIG>/tools/requirements_gate.py check --map plans/features/X/requirements-map.json --stage release --candidate <sha> --deployed-commit <sha>

Optional per-map flags: `strict_evidence: true` requires real local Git objects and candidate-bound
repo artifact paths. `slice_graph_path: "slice-graph.json"` enables static dependency/write-scope
checks. These do not prove semantic completeness or live deployment identity by themselves.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import subprocess
import fnmatch
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
            "deployed_commit": "", "strict_evidence": False, "slice_graph_path": "", "additional_requirements": [],
            "items": extract(raw.decode("utf-8-sig"))}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created {out}: {len(data['items'])} source lines need classification; PM must add prose-only constraints manually.")
    return 0


def check(args) -> int:
    p = Path(args.map)
    data = json.loads(p.read_text(encoding="utf-8"))
    errors: list[str] = []
    strict = bool(args.strict_evidence or data.get("strict_evidence") is True)
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
    reqids = {}
    scoped_required = []
    owners = set()
    source_ids = set(identities)
    manual_seen = {}  # Several atomic requirements may legitimately come from the same prose line.
    manual = data.get("additional_requirements", [])
    if not isinstance(manual, list):
        errors.append("additional_requirements must be array")
        manual = []
    for n, row in enumerate(manual, 1):
        sid = row.get("source_id", f"MANUAL_{n}")
        if sid in source_ids:
            errors.append(f"manual source_id collides with generated inventory: {sid}")
        signature = (row.get("source_line"), row.get("source_text_sha256"))
        if sid in manual_seen and manual_seen[sid] != signature:
            errors.append(f"manual source_id {sid} points to conflicting source lines/hashes")
        manual_seen[sid] = signature
        _same_source_row(row, lines, sid, errors, strict)
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
        owning_slice = str(row.get("slice", "")).strip()
        if owning_slice:
            owners.add(owning_slice)
        if rid in reqids and rid:
            prior = reqids[rid]
            if (str(prior.get("slice", "")).strip() != owning_slice or
                str(prior.get("acceptance", "")).strip() != str(row.get("acceptance", "")).strip() or
                prior.get("status") != row.get("status")):
                errors.append(f"{rid}: duplicate req_id with inconsistent owner/acceptance/status")
        elif rid:
            reqids[rid] = row
        if args.stage == "release" or (args.stage == "review" and row.get("slice") == args.slice):
            scoped_required.append(row)
    graph_ref = args.graph or data.get("slice_graph_path")
    if graph_ref:
        gp = Path(graph_ref)
        if not gp.is_absolute() and not args.graph:
            gp = p.parent / gp
        validate_graph(str(gp), owners, errors)
    if args.stage == "review" and not args.slice:
        errors.append("review stage requires --slice; release must check ALL slices")
    if args.stage == "review" and not scoped_required:
        errors.append(f"review scope has no requirements: {args.slice}")
    if args.stage in {"review", "release"}:
        expected = data.get("candidate_commit")
        if not args.candidate or not expected or args.candidate != expected:
            errors.append("candidate mismatch; map candidate_commit must match --candidate")
        if strict:
            verify_real_commit(Path(args.repo), args.candidate or "", errors, "candidate")
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
            if strict:
                # One existing proof must be structurally valid; avoid requiring every prior
                # obsolete note to point to a still-present artifact.
                valid_artifact = False
                for entry in bound:
                    local_errors = []
                    verify_artifact(Path(args.repo), entry, rid, local_errors)
                    if not local_errors:
                        valid_artifact = True
                        break
                if not valid_artifact:
                    errors.append(f"{rid}: no candidate-bound evidence points to an existing repo artifact")
            if row.get("live_required") and (args.stage == "release" or row.get("live_when", "preprod") == "preprod"):
                acceptable = ("live", "ui", "provider") if args.stage == "review" else ("live", "production")
                if not any(x.get("type") in acceptable for x in bound):
                    errors.append(f"{rid}: live requirement lacks evidence for stage {args.stage}")
    if args.stage == "release":
        if strict:
            verify_real_commit(Path(args.repo), args.deployed_commit or "", errors, "deployed_commit (local existence only)")
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



def _same_source_row(row: dict, source_lines: list[str], label: str, errors: list[str], strict: bool) -> None:
    """Manual inventory lines must not quietly point at edited/nonexistent source."""
    line = row.get("source_line")
    digest = row.get("source_text_sha256")
    if line is None and not digest:
        if strict:
            errors.append(f"{label}: manual requirement lacks source_line/source_text_sha256")
        return  # old hand-authored maps may contain only an acceptance ref
    if not isinstance(line, int) or isinstance(line, bool) or line < 1 or line > len(source_lines):
        errors.append(f"{label}: source_line missing/out of source range")
        return
    if not isinstance(digest, str) or sha(source_lines[line - 1].encode("utf-8")) != digest:
        errors.append(f"{label}: source_text_sha256 differs from authoritative line {line}")
    excerpt = row.get("source_excerpt")
    if excerpt is not None and not source_lines[line - 1].strip().startswith(str(excerpt)):
        errors.append(f"{label}: source_excerpt differs from authoritative line {line}")


def _overlap_scope(left: str, right: str) -> bool:
    """Conservative collision signal, not a proof that two writers are independent."""
    a, b = left.replace("\\", "/").strip("/"), right.replace("\\", "/").strip("/")
    if not a or not b:
        return True
    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
        return True
    # Be conservative about two glob patterns: ambiguous overlap is treated as unsafe.
    aw, bw = any(c in a for c in "*?["), any(c in b for c in "*?[")
    if aw and bw:
        ap = re.split(r"[\*\?\[]", a, 1)[0]
        bp = re.split(r"[\*\?\[]", b, 1)[0]
        # If disjoint directories are obvious, allow; otherwise assume overlap.
        ad, bd = ap.rsplit("/", 1)[0] if "/" in ap else "", bp.rsplit("/", 1)[0] if "/" in bp else ""
        return not (ad and bd and ad != bd and not ad.startswith(bd + "/") and not bd.startswith(ad + "/"))
    return (not aw and fnmatch.fnmatchcase(a, b)) or (not bw and fnmatch.fnmatchcase(b, a))


def validate_graph(graph_path: str, owners: set[str], errors: list[str]) -> None:
    """Optional machine-readable slice graph beside a human plan.md."""
    try:
        graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"slice graph unreadable: {exc}")
        return
    if not isinstance(graph, dict) or graph.get("schema") != "openworkflow-slices-v1" or not isinstance(graph.get("slices"), list):
        errors.append("slice graph must have schema=openworkflow-slices-v1 and slices array")
        return
    nodes = {}
    for node in graph["slices"]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"].strip():
            errors.append("slice graph node missing id")
            continue
        ident = node["id"]
        if ident in nodes:
            errors.append(f"duplicate slice id {ident}")
        nodes[ident] = node
    for owner in sorted(owners - nodes.keys()):
        errors.append(f"required slice has no graph node: {owner}")
    for ident, node in nodes.items():
        deps = node.get("depends_on", [])
        scopes = node.get("write_scope", [])
        if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps):
            errors.append(f"{ident}: depends_on must be a list of slice IDs")
            deps = []
            node["depends_on"] = deps
        if not isinstance(scopes, list) or any(not isinstance(x, str) for x in scopes):
            errors.append(f"{ident}: write_scope must be a list of paths/globs")
        if len(str(node.get("acceptance", "")).strip()) < 12:
            errors.append(f"{ident}: measurable slice acceptance missing")
        for dep in deps:
            if dep not in nodes:
                errors.append(f"{ident}: unknown dependency {dep}")
            if dep == ident:
                errors.append(f"{ident}: self dependency")
    visiting, visited, ancestors = set(), set(), {}
    def visit(n):
        if n in visiting:
            errors.append(f"dependency cycle involves {n}")
            return set()
        if n in visited:
            return ancestors[n]
        visiting.add(n)
        found = set()
        for dep in nodes[n].get("depends_on", []):
            if dep in nodes:
                found.add(dep)
                found.update(visit(dep))
        visiting.remove(n)
        visited.add(n)
        ancestors[n] = found
        return found
    for n in nodes:
        visit(n)
    ids = list(nodes)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in ancestors.get(b, set()) or b in ancestors.get(a, set()):
                continue
            sa, sb = nodes[a].get("write_scope", []), nodes[b].get("write_scope", [])
            if not (isinstance(sa, list) and isinstance(sb, list)):
                continue
            conflicts = [(x,y) for x in sa for y in sb if isinstance(x,str) and isinstance(y,str) and _overlap_scope(x,y)]
            if conflicts:
                errors.append(f"concurrent slices {a}/{b} have overlapping write scopes {conflicts[0]}; add dependency or isolate writers")


def verify_real_commit(repo: Path, commit: str, errors: list[str], label: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        errors.append(f"{label}: not a Git commit hash")
        return
    try:
        result = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        errors.append(f"{label}: could not validate Git object (git inaccessible/timeout)")
        return
    if result.returncode:
        errors.append(f"{label}: Git commit does not exist in local repository {repo}")


def verify_artifact(repo: Path, entry: dict, rid: str, errors: list[str]) -> None:
    """Do not mistake a free-form ref/claim for a verifiable artifact."""
    raw = entry.get("artifact_path")
    if not isinstance(raw, str) or not raw.strip():
        errors.append(f"{rid}: evidence lacks artifact_path; prose ref is not machine-verifiable")
        return
    if "\\" in raw or Path(raw).is_absolute():
        errors.append(f"{rid}: artifact_path must be repo-relative using forward slashes")
        return
    base = repo.resolve()
    path = (base / raw).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        errors.append(f"{rid}: evidence artifact missing/outside repo: {raw}")
        return
    expected_sha = entry.get("artifact_sha256")
    if expected_sha is not None:
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
            errors.append(f"{rid}: artifact_sha256 must be a SHA-256 hex digest")
        elif sha(path.read_bytes()).lower() != expected_sha.lower():
            errors.append(f"{rid}: evidence artifact SHA-256 differs: {raw}")



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
    c.add_argument("--graph", help="Optional JSON slice dependency/write-scope graph (see doc)")
    c.add_argument("--strict-evidence", action="store_true", help="Verify Git object and one real artifact per proven item; legacy maps require migration")
    c.add_argument("--repo", default=".", help="Repository root for strict Git/artifact checks (default: current directory)")
    a = ap.parse_args()
    return init(a) if a.command == "init" else check(a)


if __name__ == "__main__":
    sys.exit(main())
