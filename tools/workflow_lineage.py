#!/usr/bin/env python3
"""Read-only comparison of owner task ledgers across actual Git refs/worktrees.

A session or worktree switch MUST NOT silently replace the authoritative owner
inventory. This program only reports discrepancies; it never merges, changes
statuses, checks out refs, deletes files, or re-runs effects.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

SCOPE_PATH = "plans/progress_scope.json"


class LineageError(ValueError):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True,
                            encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise LineageError(f"git {args[0]}: {result.stderr.strip()[:350] or 'ошибка'}")
    return result.stdout.strip()


def repo_root(path: str | Path) -> Path:
    return Path(git(Path(path).resolve(), "rev-parse", "--show-toplevel")).resolve()


def common_dir(root: Path) -> Path:
    raw = git(root, "rev-parse", "--git-common-dir")
    return (root / raw).resolve()


def commit(root: Path, ref: str) -> str:
    # Use a resolved full object ID for git show, never interpolate a user ref into
    # a ref:path expression directly.
    if not ref or ref.startswith("-"):
        raise LineageError("некорректный Git ref")
    sha = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not len(sha) >= 40 or not all(c in "0123456789abcdef" for c in sha.lower()):
        raise LineageError("не удалось проверить Git commit")
    return sha


def safe_rel(raw: str, root: Path) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise LineageError("пустой путь карты")
    p = Path(raw)
    candidate = (p if p.is_absolute() else root / p).resolve()
    try:
        rel = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise LineageError(f"путь вне репозитория: {raw}") from exc
    if not rel or rel == ".":
        raise LineageError("пустой относительный путь")
    return rel


def read_json(raw: str, origin: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LineageError(f"битый JSON в {origin}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise LineageError(f"в {origin} отсутствует корректный tasks[]")
    return data


class Snapshot:
    def __init__(self, root: Path, ref: str | None = None):
        self.root = root
        self.sha = commit(root, ref or "HEAD")
        self.ref = ref or "working-tree"
        self.worktree = ref is None
        self.scope = read_json(self.file(SCOPE_PATH), f"{self.ref}:{SCOPE_PATH}")
        self.dirty = git(root, "status", "--porcelain", "--untracked-files=no", "--", "plans").splitlines() if self.worktree else []

    def file(self, rel: str) -> str:
        rel = safe_rel(rel, self.root)
        if self.worktree:
            path = self.root / rel
            if not path.is_file():
                raise LineageError(f"файл отсутствует в {self.ref}: {rel}")
            return path.read_text(encoding="utf-8-sig")
        return git(self.root, "show", f"{self.sha}:{rel}")

    def map_rows(self, raw: str) -> dict[str, dict] | None:
        try:
            data = json.loads(self.file(raw))
        except (LineageError, json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        rows = data.get("items", []) + data.get("additional_requirements", [])
        if not isinstance(rows, list):
            return None
        values = {}
        for row in rows:
            if isinstance(row, dict) and row.get("disposition") == "required":
                rid = str(row.get("req_id") or row.get("source_id") or "").strip()
                if rid:
                    # Only compare stable semantic state and evidence identity.
                    values[rid] = {
                        "status": row.get("status"), "slice": row.get("slice"),
                        "verification_tier": row.get("verification_tier", "engineering"),
                        "candidate_sha": row.get("candidate_sha"),
                        "artifact_path": row.get("artifact_path"),
                        "evidence": row.get("evidence"),
                    }
        return values


def indexed(rows: object, label: str) -> dict[str, dict]:
    if not isinstance(rows, list):
        raise LineageError(f"{label}: ожидается массив")
    out = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise LineageError(f"{label}: запись без ID")
        if row["id"] in out:
            raise LineageError(f"{label}: повторный ID {row['id']}")
        out[row["id"]] = row
    return out


def section_index(scope: dict) -> dict[str, dict]:
    inv = scope.get("source_inventory")
    return indexed(inv.get("sections", []), "source_inventory.sections") if isinstance(inv, dict) else {}


def compare(current: Snapshot, reference: Snapshot) -> dict:
    a, b = current.scope, reference.scope
    tasks_a = indexed(a["tasks"], "current.tasks")
    tasks_b = indexed(b["tasks"], "reference.tasks")
    inputs_a = indexed(a.get("owner_inputs", []), "current.owner_inputs")
    inputs_b = indexed(b.get("owner_inputs", []), "reference.owner_inputs")
    sec_a, sec_b = section_index(a), section_index(b)

    def only(left: dict, right: dict) -> list[str]:
        return sorted(left.keys() - right.keys())

    task_conflicts = []
    maps_to_compare: set[str] = set()
    for tid in sorted(tasks_a.keys() & tasks_b.keys()):
        x, y = tasks_a[tid], tasks_b[tid]
        compare_fields = ["status", "coverage_complete", "source_sha256", "cancelled_by_input"]
        changed = [f for f in compare_fields if x.get(f) != y.get(f)]
        try:
            xa = sorted(safe_rel(m, current.root) for m in x.get("maps", []))
            ya = sorted(safe_rel(m, reference.root) for m in y.get("maps", []))
        except LineageError as exc:
            task_conflicts.append({"id": tid, "error": str(exc)})
            continue
        if xa != ya:
            changed.append("maps")
        maps_to_compare.update(set(xa) & set(ya))
        if changed:
            task_conflicts.append({"id": tid, "fields": changed,
                                   "current_status": x.get("status"), "reference_status": y.get("status")})

    input_conflicts = []
    fields = ["kind", "source_sha256", "target_task_ids", "reconciled", "coverage_refs", "cancelled_by_input", "recorded_in"]
    for iid in sorted(inputs_a.keys() & inputs_b.keys()):
        changed = [f for f in fields if inputs_a[iid].get(f) != inputs_b[iid].get(f)]
        if changed:
            input_conflicts.append({"id": iid, "fields": changed})

    source_conflicts = []
    for sid in sorted(sec_a.keys() & sec_b.keys()):
        changed = [f for f in ("sha256", "disposition", "target_task_ids", "coverage_refs", "superseded_by_input")
                   if sec_a[sid].get(f) != sec_b[sid].get(f)]
        if changed:
            source_conflicts.append({"id": sid, "fields": changed})

    map_conflicts = []
    for m in sorted(maps_to_compare):
        x, y = current.map_rows(m), reference.map_rows(m)
        if x is None or y is None:
            map_conflicts.append({"map": m, "unavailable": "current" if x is None else "reference"})
            continue
        if x != y:
            altered = sorted(k for k in x.keys() & y.keys() if x[k] != y[k])
            map_conflicts.append({"map": m, "only_current_req_ids": only(x, y),
                                  "only_reference_req_ids": only(y, x), "changed_req_ids": altered})

    out = {
        "current": {"origin": current.ref, "sha": current.sha, "dirty_plans": current.dirty},
        "reference": {"origin": reference.ref, "sha": reference.sha, "dirty_plans": reference.dirty},
        "task_counts": {"current": len(tasks_a), "reference": len(tasks_b)},
        "only_current_task_ids": only(tasks_a, tasks_b),
        "only_reference_task_ids": only(tasks_b, tasks_a),
        "task_conflicts": task_conflicts,
        "only_current_input_ids": only(inputs_a, inputs_b),
        "only_reference_input_ids": only(inputs_b, inputs_a),
        "input_conflicts": input_conflicts,
        "only_current_section_ids": only(sec_a, sec_b),
        "only_reference_section_ids": only(sec_b, sec_a),
        "section_conflicts": source_conflicts,
        "map_conflicts": map_conflicts,
    }
    out["needs_reconciliation"] = bool(out["only_reference_task_ids"] or out["task_conflicts"]
        or out["only_reference_input_ids"] or out["input_conflicts"] or out["only_reference_section_ids"]
        or out["section_conflicts"] or out["map_conflicts"])
    out["informational_additions"] = bool(out["only_current_task_ids"] or out["only_current_input_ids"]
        or out["only_current_section_ids"])
    return out


def lineage(repo: Path, refs: list[str], worktrees: list[str]) -> list[dict]:
    root = repo_root(repo)
    current = Snapshot(root)
    results = []
    for ref in refs:
        results.append(compare(current, Snapshot(root, ref)))
    for path in worktrees:
        other_root = repo_root(path)
        if common_dir(other_root) != common_dir(root):
            raise LineageError(f"рабочая копия не принадлежит тому же Git-репозиторию: {other_root}")
        if other_root == root:
            continue
        results.append(compare(current, Snapshot(other_root)))
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only reconcile task lineage across Git refs/worktrees")
    p.add_argument("--repo", default=".", help="путь к активному Git checkout")
    p.add_argument("--compare-ref", action="append", default=[], help="точный Git ref/SHA выпуска или интеграции")
    p.add_argument("--compare-worktree", action="append", default=[], help="другая активная рабочая копия того же Git repo")
    p.add_argument("--json", action="store_true", help="структурированный отчёт")
    args = p.parse_args(argv)
    if not args.compare_ref and not args.compare_worktree:
        p.error("укажите хотя бы один --compare-ref или --compare-worktree; ничего не угадывается")
    try:
        results = lineage(Path(args.repo), args.compare_ref, args.compare_worktree)
    except (OSError, LineageError, UnicodeError) as exc:
        print("LINEAGE UNKNOWN: " + str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"comparisons": results, "needs_reconciliation": any(r["needs_reconciliation"] for r in results)},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"[{r['reference']['origin']}] current={r['task_counts']['current']} reference={r['task_counts']['reference']}")
            for key in ("only_current_task_ids", "only_reference_task_ids", "task_conflicts",
                        "only_current_input_ids", "only_reference_input_ids", "input_conflicts",
                        "only_reference_section_ids", "section_conflicts", "map_conflicts"):
                if r[key]:
                    print(f"  {key}: {json.dumps(r[key], ensure_ascii=False)}")
            if r["needs_reconciliation"]:
                print("  CONFLICT: ручная сверка доказательств и выбор интеграции, без автоматической замены")
            else:
                print("  Отсутствуют доказанные конфликты; уникальные новые поручения сохраняются")
    return 3 if any(r["needs_reconciliation"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
