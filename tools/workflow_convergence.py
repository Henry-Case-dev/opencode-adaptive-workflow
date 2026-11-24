#!/usr/bin/env python3
"""Read-only, evidence-bound detection of non-converging review trajectories.

Optional annotations live in existing progress_scope.json tasks[].convergence;
this module is NOT a second backlog, queue or verdict system. It never stops,
reruns, approves, alters files or automatically creates an Arbiter assignment.
"""
from __future__ import annotations

import hashlib
import subprocess
import re
import json
from pathlib import Path
from typing import Any

# A review cycle is an independent review of a candidate, not every intermediate
# comment, retry, docs commit, or revision of the same result.
APPROVED = {"Approved"}
REJECTED = {"Needs Fixes"}
CLOSED = {"VERIFIED", "CANCELLED"}


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()[:24]


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _blockers(review: dict) -> tuple[dict[str, dict], list[str]]:
    issues: list[str] = []
    entries = review.get("blocking_findings", [])
    if not isinstance(entries, list):
        return {}, ["blocking_findings must be an array"]
    found: dict[str, dict] = {}
    for f in entries:
        if not isinstance(f, dict):
            issues.append("finding must be an object")
            continue
        fid = f.get("id")
        if not _nonempty(fid):
            issues.append("finding id required")
            continue
        if fid in found:
            issues.append(f"duplicate finding id: {fid}")
            continue
        if f.get("classification") not in {"IN_SCOPE", "CRITICAL_OUT_OF_SCOPE"}:
            issues.append(f"{fid}: invalid blocking classification")
        if not isinstance(f.get("contract_refs"), list) or not all(_nonempty(x) for x in f["contract_refs"]):
            issues.append(f"{fid}: explicit contract_refs required (may be [] for proven critical finding)")
        if not _nonempty(f.get("evidence_ref")):
            issues.append(f"{fid}: independent reviewer evidence_ref required")
        if f.get("classification") == "IN_SCOPE" and not f.get("contract_refs"):
            issues.append(f"{fid}: IN_SCOPE requires mapped contract_refs")
        found[fid] = f
    if review.get("verdict") == "Approved" and found:
        issues.append("Approved cannot contain unresolved blocking findings")
    if review.get("verdict") == "Needs Fixes" and not found:
        issues.append("Needs Fixes must identify blocking findings")
    return found, issues


def _valid_review(r: Any) -> list[str]:
    if not isinstance(r, dict):
        return ["review must be an object"]
    problems = []
    for field in ("candidate", "review_ref", "reviewer_session"):
        if not _nonempty(r.get(field)):
            problems.append(f"missing {field}")
    if r.get("verdict") not in APPROVED | REJECTED:
        problems.append("verdict must be Approved or Needs Fixes")
    _, errors = _blockers(r)
    return problems + errors


def _trigger(tid: str, contract: str, kind: str, ids: set[str]) -> dict:
    ordered = sorted(ids)
    return {"kind": kind, "finding_ids": ordered,
            # Candidate and time deliberately excluded: same unresolved
            # dispute in a later review must not spawn another Arbiter.
            "trigger_key": _key("arbiter-v1", tid, contract, kind, ordered)}


def _material_diff(repo: Path, old: str, new: str) -> bool | None:
    """True only for a real material Git delta; None if ancestry/candidate is unknown.

    Never rely on a human-supplied `implementation_changed` flag. Git refs are
    validated and compared on the same repository; document-only evidence
    commits are intentionally not treated as another implementation attempt.
    """
    if not (re.fullmatch(r"[0-9a-fA-F]{7,40}", old or "") and
            re.fullmatch(r"[0-9a-fA-F]{7,40}", new or "")):
        return None
    def run(*args: str):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=12, check=False)
    try:
        if run("merge-base", "--is-ancestor", old, new).returncode != 0:
            return None
        diff = run("diff", "--name-only", "-z", old, new, "--")
        if diff.returncode != 0:
            return None
        paths = [s.decode("utf-8", "surrogateescape").replace("\\", "/")
                 for s in diff.stdout.split(b"\0") if s]
    except (OSError, subprocess.TimeoutExpired):
        return None
    def executable(name: str) -> bool:
        lower=name.lower()
        if name.startswith("plans/"):
            return False  # a plan change is NOT an implementation attempt
        if lower.endswith((".md", ".txt", ".rst", ".adoc")):
            return False
        return True  # source, tests, configuration, build, binary, assets
    return any(executable(name) for name in paths)


def assess_task(task: dict, repo: Path | None = None) -> dict:
    """Pure decision; never infer code changed or reviewer honesty from timestamps."""
    tid = str(task.get("id", ""))
    if task.get("status") in CLOSED:
        return {"task_id": tid, "state": "CLOSED", "triggers": [], "issues": []}
    c = task.get("convergence")
    if c is None:
        return {"task_id": tid, "state": "UNRECORDED", "triggers": [], "issues": []}
    if not isinstance(c, dict) or not isinstance(c.get("contract_digest"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", c["contract_digest"]):
        return {"task_id": tid, "state": "INCOMPLETE", "triggers": [],
                "issues": ["convergence.contract_digest is required"]}
    rows = c.get("reviews")
    if not isinstance(rows, list):
        return {"task_id": tid, "state": "INCOMPLETE", "triggers": [],
                "issues": ["convergence.reviews must be an array"]}
    issues = []
    real = []
    seen = set()
    for r in rows:
        errors = _valid_review(r)
        if isinstance(r, dict) and r.get("contract_digest") != c["contract_digest"]:
            errors.append("review contract_digest differs from current frozen contract; do not compare revisions")
        if errors:
            issues.extend(errors)
            continue
        identity = (r["candidate"], r["review_ref"], r["reviewer_session"])
        if identity in seen:
            continue  # duplicate reports do not create synthetic iterations
        seen.add(identity)
        real.append(r)
    if issues:
        return {"task_id": tid, "state": "INCOMPLETE", "triggers": [], "issues": sorted(set(issues))}
    if not real and c.get("manual_escalation") is None:
        return {"task_id": tid, "state": "NO_REVIEWS", "triggers": [], "issues": []}
    contract = c["contract_digest"]
    triggers = []
    latest = real[-1] if real else {"blocking_findings": [], "verdict": "Approved"}
    now_blocks, _ = _blockers(latest)
    # Only candidates whose implementation actually changed count as another
    # corrective attempt. A metadata-only SHA, a repeated review, or a model
    # retry is NOT evidence of failed correction.
    # One failed correction can be an ordinary engineering iteration. Call
    # Arbiter only after two *material* corrective attempts with the exact
    # same unresolved blocking set and no observed improvement. Earlier
    # intervention remains possible through a substantiated dispute/manual
    # escalation, without reducing review standards or imposing a hard limit.
    if len(real) >= 3 and repo is not None:
        before, middle, cur = real[-3:]
        old_blocks, _ = _blockers(before)
        mid_blocks, _ = _blockers(middle)
        cur_blocks, _ = _blockers(cur)
        d1 = _material_diff(repo, before["candidate"], middle["candidate"])
        d2 = _material_diff(repo, middle["candidate"], cur["candidate"])
        if (d1 is True and d2 is True and old_blocks and
                set(old_blocks) == set(mid_blocks) == set(cur_blocks) and
                cur.get("verdict") == "Needs Fixes"):
            triggers.append(_trigger(tid, contract, "SAME_BLOCKER_AFTER_CORRECTIONS", set(cur_blocks)))
        elif (d1 is None or d2 is None) and old_blocks and mid_blocks and cur_blocks:
            # Missing ancestry is a diagnostic uncertainty, not a reason to
            # suppress an independent explicit evidence-backed escalation.
            issues.append("candidate ancestry/material diff unknown; reconcile before automatic cycle inference")
    # A blocker fixed in one independently reviewed candidate and then
    # reintroduced in a later candidate is oscillation, not a new task.
    if len(real) >= 3 and repo is not None:
        a, b, d = real[-3:]
        if (_material_diff(repo, a["candidate"], b["candidate"]) is True and
                _material_diff(repo, b["candidate"], d["candidate"]) is True):
            old, _ = _blockers(a)
            mid, _ = _blockers(b)
            new, _ = _blockers(d)
            reopened = (set(old) & set(new)) - set(mid)
            if reopened:
                triggers.append(_trigger(tid, contract, "OSCILLATING_CONTRACT", reopened))
    # Explicit contest between reviewer and builder: never auto-disqualify the
    # reviewer; Arbiter may only narrow next proof needed, not overrule it.
    dispute = c.get("dispute")
    if dispute is not None:
        if not isinstance(dispute, dict) or not all(_nonempty(dispute.get(k))
                    for k in ("finding_id", "builder_evidence_ref", "reviewer_evidence_ref")):
            issues.append("dispute requires finding_id and both independent evidence refs")
        elif dispute["finding_id"] in now_blocks:
            triggers.append(_trigger(tid, contract, "CONTRACT_DISPUTE", {dispute["finding_id"]}))
    # The reviewer must not silently promote backlog or untraceable scope
    # to release blockers. New critical risks remain actionable separately.
    # A supported external explicit request can always trigger a one-off audit
    # of a concrete repeated symptom even when old review records are partial.
    manual = c.get("manual_escalation")
    if manual is not None:
        if not isinstance(manual, dict) or not all(_nonempty(manual.get(k)) for k in ("reason", "evidence_ref")):
            issues.append("manual escalation needs a concrete reason and evidence_ref")
        else:
            triggers.append(_trigger(tid, contract, "EVIDENCE_BACKED_ESCALATION", {manual["evidence_ref"]}))
    # Dedupe equivalent triggers and make pending/resolved cases idempotent.
    unique = {t["trigger_key"]: t for t in triggers}
    arb = c.get("arbiter", {})
    if arb is None: arb = {}
    if not isinstance(arb, dict):
        issues.append("convergence.arbiter must be an object")
        arb = {}
    state = arb.get("state")
    if state not in (None, "OPEN", "RESOLVED"):
        issues.append("arbiter.state must be OPEN or RESOLVED")
    if state == "OPEN" and (not _nonempty(previous := arb.get("trigger_key")) or
                            not _nonempty(arb.get("assignment_id"))):
        issues.append("OPEN Arbiter case needs trigger_key and durable assignment_id")
    if state == "RESOLVED" and not _nonempty(arb.get("decision_ref")):
        issues.append("RESOLVED Arbiter case needs original decision_ref")
    previous = arb.get("trigger_key")
    active = []
    suppressed = []
    for key, t in unique.items():
        if key == previous:
            suppressed.append(t)
        elif state == "OPEN":
            # Do not create concurrent arbiters for the same task. New evidence
            # is routed to the existing case, never a parallel subagent.
            suppressed.append(t)
        else:
            active.append(t)
    unknown_only = bool(issues) and all("ancestry/material diff unknown" in x for x in issues)
    if issues and not unknown_only:
        result_state = "NEEDS_EVIDENCE"
        active = []
    elif active:
        result_state = "CALL_ARBITER"
    elif state == "OPEN":
        result_state = "ARBITER_CASE_OPEN"
    elif unknown_only:
        result_state = "NEEDS_EVIDENCE"
    else:
        result_state = "STABLE"
    return {"task_id": tid, "state": result_state, "triggers": active,
            "suppressed": suppressed, "issues": issues, "review_count": len(real)}


def audit_scope(scope: dict, repo: Path | None = None) -> dict:
    if not isinstance(scope, dict) or not isinstance(scope.get("tasks"), list):
        raise ValueError("expected existing progress_scope tasks[]")
    recorded = [t for t in scope["tasks"] if isinstance(t, dict) and t.get("convergence") is not None]
    rows = [assess_task(t, repo=repo) for t in recorded]
    return {"needs_arbiter": [x for x in rows if x["state"] == "CALL_ARBITER"],
            "needs_reconciliation": [x for x in rows if x["state"] in ("INCOMPLETE", "NEEDS_EVIDENCE")],
            "open_cases": [x for x in rows if x["state"] == "ARBITER_CASE_OPEN"],
            "task_results": rows, "legacy_unrecorded": len(scope["tasks"]) - len(recorded)}


def main() -> int:
    import argparse
    p=argparse.ArgumentParser(description="Read-only Arbiter trigger from current durable task rows")
    p.add_argument("--scope-file", required=True)
    args=p.parse_args()
    try:
        root=json.loads(Path(args.scope_file).read_text(encoding="utf-8-sig"))
        result=audit_scope(root, repo=Path(args.scope_file).resolve().parent.parent)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(json.dumps({"state":"UNKNOWN","error":str(e)},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0  # Arbiter triggers do not block the independent delivery queue.


if __name__ == "__main__":
    raise SystemExit(main())
