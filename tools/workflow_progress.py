#!/usr/bin/env python3
"""Read-only progress snapshot of explicitly selected active requirement maps.

Never infers task completion from files/tests/commits alone. Only VERIFIED requirements
count as completed; a slice is completed if every mapped requirement is VERIFIED.
Use one or more --map arguments to define the entire active reporting scope.
Run from the project repository root; maps are project files, this script is global.
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import sys

SCHEMA = "openworkflow-coverage-v1"
SCOPE_SCHEMA = "openworkflow-progress-scope-v1"
TASK_STATES = {"PENDING", "BUILDING", "REVIEW", "READY_FOR_LIVE", "WAITING_EXTERNAL", "BLOCKED_REAL_HUMAN_GATE", "FAILED", "VERIFIED", "CANCELLED"}
KNOWN_STATES = {"PENDING", "BUILDING", "REVIEW", "VERIFIED", "FAILED", "BLOCKED_REAL_HUMAN_GATE", "READY_FOR_LIVE"}


def audit_execution_queue(tasks: list[dict], project: Path | None = None) -> dict:
    """Read-only structural guard for the *existing* tasks[] execution queue.

    Mapping, queue admission and execution are distinct states. Never infer a
    running worker from BUILDING or an assigned position, and never mutate the
    owner's queue. Semantic dependency/slice and priority decisions belong to
    PM/Orchestrator, which have the relevant project sources.
    """
    open_rows = [row for row in tasks if isinstance(row, dict) and
                 str(row.get("status", "")).upper() not in {"VERIFIED", "CANCELLED"}]
    all_ids = {row.get("id") for row in tasks if isinstance(row, dict)}
    positions: dict[int, str] = {}
    issues: list[str] = []
    entries: list[dict] = []
    edges: dict[str, list[str]] = {}
    for row in open_rows:
        tid = str(row.get("id") or "(no id)")
        pos, priority, blockers = row.get("queue_position"), row.get("priority"), row.get("blocked_by")
        if type(pos) is not int or pos <= 0:
            issues.append(f"{tid}: отсутствует положительный queue_position")
        elif pos in positions:
            issues.append(f"{tid}: queue_position={pos} занят задачей {positions[pos]}")
        else:
            positions[pos] = tid
        if not (isinstance(priority, str) and re.fullmatch(r"P[0-9]", priority.upper()) or
                type(priority) is int and 0 <= priority <= 9):
            issues.append(f"{tid}: отсутствует/некорректен priority (P0..P9 или 0..9)")
        if not isinstance(blockers, list) or any(not isinstance(b, str) or not b.strip() for b in blockers):
            issues.append(f"{tid}: blocked_by должен быть явным списком, включая []")
            blockers = []
        if len(set(blockers)) != len(blockers):
            issues.append(f"{tid}: дублирующиеся blocked_by")
        if tid in blockers:
            issues.append(f"{tid}: задача блокирует саму себя")
        # A non-task dependency (an internal slice, architecture contract or
        # external event) needs a trace to an existing project artifact. This
        # does not prove the dependency is necessary: PM reviews that.
        refs = row.get("blocker_sources", {})
        if refs is None:
            refs = {}
        if not isinstance(refs, dict):
            issues.append(f"{tid}: blocker_sources должен быть объектом")
            refs = {}
        for blocker in blockers:
            if blocker not in all_ids:
                ref = refs.get(blocker)
                if not isinstance(ref, str) or not ref.strip():
                    issues.append(f"{tid}: для зависимости {blocker} нет blocker_sources")
                elif project is not None:
                    source_path = _project_path(ref, project)
                    if not source_path.is_file():
                        issues.append(f"{tid}: blocker_sources для {blocker} не найден: {ref}")
        edges[tid] = [b for b in blockers if b in all_ids and b != tid]
        entries.append({"task_id": tid, "queue_position": pos, "priority": priority,
                        "blocked_by": blockers, "status": str(row.get("status", ""))})
    # Detect cycles between in-scope tasks; a queue position cannot rescue one.
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str, chain: list[str]) -> None:
        if node in visiting:
            issues.append("цикл зависимостей: " + " -> ".join(chain + [node]))
            return
        if node in visited:
            return
        visiting.add(node)
        for next_node in edges.get(node, []):
            if next_node in edges:
                visit(next_node, chain + [node])
        visiting.remove(node)
        visited.add(node)
    for task_id in edges:
        visit(task_id, [])
    entries.sort(key=lambda x: x["queue_position"] if type(x["queue_position"]) is int and x["queue_position"] > 0 else float("inf"))
    return {"complete": not issues, "open_tasks": len(open_rows),
            "registered": len(positions), "issues": issues, "entries": entries}


def optional_owner_check(row: dict) -> bool:
    return row.get("verification_tier", "engineering") == "owner_optional"



def _load_maps(paths: list[str], namespaces: dict[Path, str] | None = None, project: Path | None = None) -> dict:
    statuses: dict[tuple[str, str], str] = {}
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    duplicates = 0
    optional_statuses = {}
    paths_seen = set()
    ordered_paths: list[str] = []
    for raw_path in paths:
        p = _project_path(raw_path, project or Path.cwd())
        if p in paths_seen:
            continue
        paths_seen.add(p)
        ordered_paths.append(str(p))
        if not p.is_file():
            raise ValueError(f"карта не найдена: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"карта не читается: {p}: {exc}") from exc
        if data.get("schema") != SCHEMA:
            raise ValueError(f"неподдерживаемый формат карты: {p}")
        a = data.get("items", [])
        b = data.get("additional_requirements", [])
        if not isinstance(a, list) or not isinstance(b, list):
            raise ValueError(f"некорректные разделы карты: {p}")
        for i, row in enumerate(a + b, 1):
            if not isinstance(row, dict):
                raise ValueError(f"некорректная строка {i}: {p}")
            if row.get("disposition") != "required":
                continue
            rid = str(row.get("req_id") or row.get("source_id") or "").strip()
            owner = str(row.get("slice") or "").strip()
            state = str(row.get("status") or "UNKNOWN").strip().upper()
            if not rid or not owner:
                raise ValueError(f"обязательное требование без ID/владельца (строка {i}): {p}")
            if state not in KNOWN_STATES:
                raise ValueError(f"неизвестный статус {state} для требования {rid}: {p}")
            if row.get("verification_tier", "engineering") not in {"engineering", "owner_optional"}:
                raise ValueError(f"неизвестный verification_tier у {rid}: {p}")
            # The progress reader must not trust an unchecked optional label.
            # Require the same simple preconditions as the gate before excluding a row.
            if optional_owner_check(row):
                links = row.get("engineering_req_ids")
                if (row.get("live_required", False) is not False or
                    not row.get("owner_policy_ref") or
                    len(str(row.get("owner_optional_reason", "")).strip()) < 16 or
                    not isinstance(links, list) or not links):
                    raise ValueError(f"непроверенная классификация необязательной проверки {rid}: {p}")
                key = (str(p), rid)
                if key in statuses:
                    raise ValueError(f"одно требование не может быть и обязательным, и добровольным: {rid} в {p}")
                if key in optional_statuses and optional_statuses[key] != state:
                    raise ValueError(f"у необязательной проверки два статуса: {rid} в {p}")
                optional_statuses[key] = state
                if state in {"FAILED", "BLOCKED_REAL_HUMAN_GATE"}:
                    raise ValueError(f"проблема в необязательной проверке {rid}: требуется анализ дефекта")
                continue
            key = (str(p), rid)
            if key in optional_statuses:
                raise ValueError(f"одно требование не может быть и обязательным, и добровольным: {rid} в {p}")
            if key in statuses:
                duplicates += 1
                # Contradictory duplicates are data quality defects, not two requirements.
                if statuses[key] != state:
                    raise ValueError(f"у одного требования два статуса: {rid} в {p}")
            else:
                statuses[key] = state
            owner_key = (namespaces.get(p, "legacy") if namespaces else "legacy", owner)
            owners[owner_key].add(key)
    # Reference integrity for all optional rows, including across two maps of one epic:
    # only listed engineering requirements can justify treating feedback as optional.
    for raw_path in ordered_paths:
        p = Path(raw_path)
        map_engineering_rids = {key[1] for key in statuses if key[0] == str(p)}
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        for row in data.get("items", []) + data.get("additional_requirements", []):
            if row.get("disposition") == "required" and optional_owner_check(row):
                if not all(isinstance(x, str) and x in map_engineering_rids
                           for x in row["engineering_req_ids"]):
                    raise ValueError(f"owner_optional без инженерной пары: {row.get('req_id')} в {p}")
                policy = Path(row["owner_policy_ref"])
                if not policy.is_absolute():
                    policy = (project or Path.cwd()) / policy
                if not policy.is_file():
                    raise ValueError(f"не найден owner_policy_ref у {row.get('req_id')}: {policy}")
    if not statuses:
        raise ValueError("нет обязательных требований в выбранных картах")
    stat_counts = Counter(statuses.values())
    completed_req = stat_counts.get("VERIFIED", 0)
    done_slices = sum(all(statuses[key] == "VERIFIED" for key in keys) for keys in owners.values())
    return {
        "maps": ordered_paths,
        "slices": {"done": done_slices, "total": len(owners)},
        "requirements": {"done": completed_req, "total": len(statuses)},
        "statuses": dict(sorted(stat_counts.items())),
        "owner_optional_checks": {"total": len(optional_statuses),
                                  "pending": sum(x != "VERIFIED" for x in optional_statuses.values()),
                                  "verified": sum(x == "VERIFIED" for x in optional_statuses.values())},
        "unverified": len(statuses) - completed_req,
        "duplicates_collapsed": duplicates,
    }



# Source-wide reverse inventory: a section can be PENDING, mapped, or historically
# disposed, but it cannot disappear because no task/map was ever created for it.
# This deliberately checks STRUCTURE, not whether PM understood every clause.
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _project_path(raw: str, project: Path) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else project / path).resolve()


def source_sections(source: Path, level: int | None = None) -> tuple[int, list[dict]]:
    """Top-level Markdown sections, stable by heading text + duplicate ordinal.

    Ignore headings in fenced code. A single document-title heading is skipped in
    favor of the next level when the document has many second-level sections.
    """
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    headings = []
    fence_char, fence_len = None, 0
    for number, line in enumerate(lines, 1):
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
            elif fence_char == marker[0] and len(marker) >= fence_len:
                fence_char, fence_len = None, 0
            continue
        if fence_char is not None:
            continue
        found = HEADING.match(line)
        if found:
            headings.append((number, len(found.group(1)), found.group(2).strip()))
    if level is None:
        level = min((entry[1] for entry in headings), default=1)
        if sum(h[1] == level for h in headings) == 1 and any(h[1] == level + 1 for h in headings):
            level += 1
    if level not in range(1, 7):
        raise ValueError("heading_level должен быть от 1 до 6")
    sections = []
    starts = [(n, title) for n, depth, title in headings if depth == level]
    if not starts and any(x.strip() for x in lines):
        starts = [(1, "(no heading)")]
    # Material preceding the first selected heading can itself contain owner
    # instructions; never silently drop it (e.g. a document-level preface).
    if starts and starts[0][0] > 1 and any(x.strip() for x in lines[:starts[0][0]-1]):
        starts.insert(0, (1, "(preamble)"))
    seen = Counter()
    for index, (start, title) in enumerate(starts):
        end = starts[index+1][0]-1 if index+1 < len(starts) else len(lines)
        norm = re.sub(r"\s+", " ", title).casefold()
        seen[norm] += 1
        ident = f"H{level}:{hashlib.sha256(norm.encode('utf8')).hexdigest()[:12]}:{seen[norm]}"
        contents = "\n".join(lines[start-1:end]).encode("utf8")
        sections.append({"id": ident, "heading": title, "start_line": start, "end_line": end,
                         "sha256": hashlib.sha256(contents).hexdigest()})
    return level, sections


def inventory_template(source: Path, project: Path, level: int | None = None) -> dict:
    level, sections = source_sections(source, level)
    try:
        source_name = str(source.relative_to(project))
    except ValueError:
        source_name = str(source)
    return {"source_path": source_name, "heading_level": level, "sections": [
        {"id": section["id"], "sha256": section["sha256"],
         "disposition": "unreviewed", "target_task_ids": [],
         "coverage_refs": [], "reason": ""}
        for section in sections]}


def audit_source_inventory(scope: dict, scope_path: Path, task_by_id: dict,
                           inputs_by_id: dict, stale: list[str]) -> dict:
    """Bidirectional check of all source sections and their explicit dispositions.

    No automatic task creation, completion, status adjustment or overwrite.
    An old heading stays historical only when there is a documented reason.
    """
    project = scope_path.parent.parent if scope_path.parent.name == "plans" else scope_path.parent
    raw = scope.get("source_inventory")
    default_source = project / "plans" / "current_task.md"
    if raw is None:
        if default_source.is_file():
            stale.append("current_task.md существует, но source_inventory не создан: обратный учёт разделов отсутствует")
        return {"total": 0, "accounted": 0, "unreviewed": [], "drift": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        stale.append("source_inventory имеет неправильный формат")
        return {"total": 0, "accounted": 0, "unreviewed": [], "drift": []}
    sp = raw.get("source_path")
    if not isinstance(sp, str) or not sp.strip():
        stale.append("source_inventory без source_path")
        return {"total": 0, "accounted": 0, "unreviewed": [], "drift": []}
    source = _project_path(sp, project)
    if not source.is_file():
        stale.append(f"source_inventory: не найден исходный файл {sp}")
        return {"total": 0, "accounted": 0, "unreviewed": [], "drift": []}
    if default_source.is_file() and source != default_source.resolve():
        stale.append("source_inventory не охватывает актуальный plans/current_task.md")
    try:
        level, observed = source_sections(source, raw.get("heading_level"))
    except (OSError, UnicodeError, ValueError) as exc:
        stale.append(f"source_inventory: не удалось прочитать исходный файл: {exc}")
        return {"total": 0, "accounted": 0, "unreviewed": [], "drift": []}
    registered = {}
    for entry in raw["sections"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            stale.append("source_inventory: некорректная запись раздела")
            continue
        sid = entry["id"]
        if sid in registered:
            stale.append(f"source_inventory: повторный ID раздела {sid}")
        registered[sid] = entry
    actual = {section["id"]: section for section in observed}
    unmatched, drift, accounted = [], [], 0
    for sid, section in actual.items():
        entry = registered.get(sid)
        if not entry:
            unmatched.append(f"{section['heading']} (стр. {section['start_line']})")
            stale.append(f"не учтён раздел current_task: {section['heading']} (стр. {section['start_line']})")
            continue
        if entry.get("sha256") != section["sha256"]:
            drift.append(sid)
            stale.append(f"раздел current_task изменён: {section['heading']} - повторить сверку")
            continue
        mode = entry.get("disposition")
        targets = entry.get("target_task_ids", [])
        if not isinstance(targets, list) or not all(isinstance(t, str) and t in task_by_id for t in targets):
            stale.append(f"раздел {sid}: отсутствующая или неверная связанная задача")
            continue
        refs = entry.get("coverage_refs", [])
        if mode == "mapped":
            if not targets or not isinstance(refs, list) or not refs:
                stale.append(f"раздел {sid}: нет задачи и проверяемой карты покрытия")
                continue
            owned_maps = {_project_path(m, project) for tid in targets for m in task_by_id[tid].get("maps", [])}
            if any(not isinstance(m, str) or _project_path(m, project) not in owned_maps
                   or not _project_path(m, project).is_file() for m in refs):
                stale.append(f"раздел {sid}: карта покрытия отсутствует или не принадлежит связанной задаче")
                continue
        elif mode == "superseded":
            by = entry.get("superseded_by_input")
            reason = str(entry.get("reason") or "").strip()
            if len(reason) < 16 or by not in inputs_by_id or not inputs_by_id[by].get("reconciled"):
                stale.append(f"раздел {sid}: устаревание не подтверждено более новым поручением")
                continue
        elif mode == "context":
            if len(str(entry.get("reason") or "").strip()) < 24:
                stale.append(f"раздел {sid}: объявлен контекстом без конкретного основания")
                continue
        elif mode == "excluded":
            by = entry.get("excluded_by_input")
            if len(str(entry.get("reason") or "").strip()) < 16 or by not in inputs_by_id or inputs_by_id[by].get("kind") != "cancellation" or not inputs_by_id[by].get("reconciled"):
                stale.append(f"раздел {sid}: исключение без прямого подтверждения владельца")
                continue
        else:
            unmatched.append(f"{section['heading']} (стр. {section['start_line']})")
            stale.append(f"раздел {sid} не классифицирован ({mode})")
            continue
        accounted += 1
    for sid in registered.keys() - actual.keys():
        drift.append(sid)
        stale.append(f"source_inventory содержит устаревший/удалённый раздел {sid}")
    return {"total": len(observed), "accounted": accounted, "unreviewed": unmatched, "drift": drift,
            "heading_level": level}



def audit_input_links(raw_inputs: list[dict], tasks: list[dict], project: Path,
                      stale: list[str]) -> None:
    """A boolean `reconciled` cannot substitute for source->requirement evidence.

    This is a structural check. PM still independently audits all clauses and the
    meaning of the linked requirements. The function is strictly read-only.
    """
    task_by_id = {row.get("id"): row for row in tasks if isinstance(row, dict)}
    map_cache: dict[Path, dict | None] = {}
    for entry in raw_inputs:
        if not entry.get("reconciled") or entry.get("kind") == "cancellation":
            continue
        iid = entry["id"]
        targets = entry.get("target_task_ids", [])
        links = entry.get("coverage_refs", [])
        recorded = entry.get("recorded_in")
        documented_decision = (entry.get("kind") == "decision" and isinstance(recorded, str)
                               and bool(recorded.strip()) and _project_path(recorded, project).is_file())
        if not isinstance(links, list):
            stale.append(f"{iid}: coverage_refs должен быть массивом")
            continue
        if not links and not documented_decision:
            stale.append(f"{iid}: reconciled=true без source→requirement refs или доказанного решения")
            continue
        covered_targets: set[str] = set()
        for link in links:
            if not isinstance(link, dict):
                stale.append(f"{iid}: некорректный coverage_ref")
                continue
            tid, m, ids = link.get("task_id"), link.get("map"), link.get("req_ids")
            if tid not in targets or tid not in task_by_id:
                stale.append(f"{iid}: coverage_ref ссылается на чужую/несуществующую задачу {tid}")
                continue
            if not isinstance(m, str) or not m.strip() or not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x.strip() for x in ids):
                stale.append(f"{iid}: coverage_ref без map и req_ids")
                continue
            pmap = _project_path(m, project)
            if pmap not in {_project_path(p, project) for p in task_by_id[tid].get("maps", [])}:
                stale.append(f"{iid}: карта {m} не принадлежит задаче {tid}")
                continue
            if pmap not in map_cache:
                try:
                    map_cache[pmap] = json.loads(pmap.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    map_cache[pmap] = None
            data = map_cache[pmap]
            if not isinstance(data, dict) or data.get("schema") != SCHEMA:
                stale.append(f"{iid}: отсутствует/повреждена карта {m}")
                continue
            items = data.get("items", []) + data.get("additional_requirements", [])
            exists = {str(r.get("req_id")) for r in items if isinstance(r, dict) and r.get("disposition") == "required"}
            missing = [rid for rid in ids if rid not in exists]
            if missing:
                stale.append(f"{iid}: в {m} не найдены обязательные требования {', '.join(missing)}")
            else:
                covered_targets.add(tid)
        if not documented_decision:
            for tid in targets:
                if tid not in covered_targets:
                    stale.append(f"{iid}: нет доказанной связи с обязательными требованиями задачи {tid}")


def _load_scope(scope_path: str, label: str) -> dict:
    """Explicit inventory of ALL in-scope owner requests, not just requirements maps.

    File paths are relative to the project cwd or absolute. The owner-created
    current_task.md remains read-only; root-input tasks require durable snapshots.
    A changed source checksum invalidates the previous percentage until reconciliation.
    """
    path = Path(scope_path).resolve()
    if not path.is_file():
        raise ValueError(f"нет реестра задач: {path}")
    try:
        root = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"реестр не читается: {path}: {exc}") from exc
    if root.get("schema") != SCOPE_SCHEMA or not isinstance(root.get("tasks"), list):
        raise ValueError("неподдерживаемый формат реестра задач")
    project = path.parent.parent if path.parent.name == "plans" else path.parent
    tasks = root["tasks"]
    if not tasks:
        raise ValueError("реестр задач пуст; проценты не подтверждены")
    task_ids, map_paths, uncovered, stale = set(), [], [], []
    raw_inputs = root.get("owner_inputs", [])
    if not isinstance(raw_inputs, list):
        raise ValueError("owner_inputs должен быть массивом")
    input_by_id: dict[str, dict] = {}
    pending_inputs: list[str] = []
    for row in raw_inputs:
        if not isinstance(row, dict):
            raise ValueError("элемент owner_inputs должен быть объектом")
        iid = str(row.get("id") or "").strip()
        if not iid or iid in input_by_id:
            raise ValueError(f"пустой или повторный ID входящего поручения: {iid!r}")
        input_by_id[iid] = row
        kind = row.get("kind")
        if kind not in {"new_task", "amendment", "correction", "cancellation", "decision"}:
            raise ValueError(f"неизвестный тип входящего поручения {iid}: {kind}")
        targets = row.get("target_task_ids")
        if not isinstance(targets, list) or len(set(map(str, targets))) != len(targets) or not all(isinstance(x, str) and x.strip() for x in targets):
            raise ValueError(f"некорректные связи с задачами у {iid}")
        if not isinstance(row.get("reconciled"), bool):
            raise ValueError(f"reconciled должен быть boolean у {iid}")
        sp, fp = row.get("source_path"), row.get("source_sha256")
        if not isinstance(sp, str) or not sp.strip() or not isinstance(fp, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fp):
            stale.append(f"{iid}: нет проверяемого источника входящего поручения")
        else:
            source = _project_path(sp, project)
            if not source.is_file():
                stale.append(f"{iid}: потерян исходный текст/снимок ({sp})")
            elif hashlib.sha256(source.read_bytes()).hexdigest().lower() != fp.lower():
                stale.append(f"{iid}: исходный текст изменился после регистрации")
        if not row["reconciled"]:
            pending_inputs.append(iid)
    # Detect source snapshots that were written but never registered in the shared index.
    inbox = path.parent / "owner_inputs"
    if inbox.is_dir():
        indexed = {_project_path(row["source_path"], project) for row in raw_inputs
                   if isinstance(row.get("source_path"), str) and row["source_path"].strip()}
        for orphan in sorted(inbox.glob("*.md")):
            if orphan.resolve() not in indexed:
                stale.append(f"неучтённое входящее поручение: {orphan}")
    completed_with_maps: list[tuple[str, list[str]]] = []
    map_namespaces: dict[Path, str] = {}
    status_count = Counter()
    for row in tasks:
        if not isinstance(row, dict):
            raise ValueError("запись задачи должна быть объектом")
        tid = str(row.get("id") or "").strip()
        if not tid or tid in task_ids:
            raise ValueError(f"пустой или повторный ID задачи: {tid!r}")
        task_ids.add(tid)
        status = str(row.get("status") or "").strip().upper()
        if status not in TASK_STATES:
            raise ValueError(f"неверный статус задачи {tid}: {status}")
        status_count[status] += 1
        sp, fingerprint = row.get("source_path"), row.get("source_sha256")
        if not isinstance(sp, str) or not sp.strip() or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
            stale.append(f"{tid}: нет проверяемого снимка исходного задания")
        else:
            source = _project_path(sp, project)
            if not source.is_file():
                stale.append(f"{tid}: исходный файл не найден ({sp})")
            elif hashlib.sha256(source.read_bytes()).hexdigest().lower() != fingerprint.lower():
                stale.append(f"{tid}: исходное задание изменилось после последнего учёта")
        mp = row.get("maps", [])
        if not isinstance(mp, list) or not all(isinstance(x, str) and x.strip() for x in mp):
            raise ValueError(f"неверный список карт для задачи {tid}")
        # New tasks count immediately; requirements count only after PM maps them.
        if status != "CANCELLED" and (not row.get("coverage_complete") or not mp):
            uncovered.append(tid)
        if status == "CANCELLED":
            cancellation_id = row.get("cancelled_by_input")
            cancellation = input_by_id.get(cancellation_id)
            if (not cancellation or cancellation.get("kind") != "cancellation"
                or not cancellation.get("reconciled") or tid not in cancellation.get("target_task_ids", [])):
                stale.append(f"{tid}: отмена не подтверждена привязанным решением владельца")
            continue  # Retain history, exclude deliberately cancelled requirements from active totals.
        if status == "VERIFIED" and mp:
            completed_with_maps.append((tid, [str(_project_path(m, project)) for m in mp]))
        for raw_map in mp:
            resolved = _project_path(raw_map, project)
            if resolved in map_namespaces and map_namespaces[resolved] != tid:
                raise ValueError(f"одна карта привязана к двум разным поручениям: {raw_map}")
            map_namespaces[resolved] = tid
            map_paths.append(str(resolved))
    for iid, entry in input_by_id.items():
        targets = entry["target_task_ids"]
        missing_targets = [tid for tid in targets if tid not in task_ids]
        if missing_targets and entry["reconciled"]:
            stale.append(f"{iid}: поручение ссылается на несуществующие задачи: {', '.join(missing_targets)}")
        if entry["reconciled"] and not targets:
            # A global policy/decision may have no feature owner, but must point to its recorded outcome.
            ref = entry.get("recorded_in")
            if entry["kind"] != "decision" or not isinstance(ref, str) or not ref.strip() or not _project_path(ref, project).is_file():
                stale.append(f"{iid}: закрытое поручение не связано с задачей или документированным решением")
        if entry["reconciled"] and targets and entry["kind"] != "cancellation":
            for tid in targets:
                target = next((t for t in tasks if t.get("id") == tid), None)
                if target is not None and target.get("status") != "CANCELLED" and (
                    not target.get("coverage_complete") or not target.get("maps")
                ):
                    stale.append(f"{iid}: закрыт учёт входа, но карта требований задачи {tid} неполная")
    for tid, mapped_paths in completed_with_maps:
        if tid in uncovered:
            stale.append(f"{tid}: задача помечена VERIFIED без завершённого учёта требований")
            continue
        try:
            evidence = _load_maps(mapped_paths, project=project)
            if evidence["requirements"]["done"] != evidence["requirements"]["total"]:
                stale.append(f"{tid}: задача VERIFIED, но в карте остались неподтверждённые требования")
        except ValueError as exc:
            stale.append(f"{tid}: ошибочная карта завершённой задачи: {exc}")
    project = path.parent.parent if path.parent.name == "plans" else path.parent
    audit_input_links(raw_inputs, tasks, project, stale)
    section_coverage = audit_source_inventory(root, path, {t["id"]: t for t in tasks}, input_by_id, stale)
    # Statuses are not inferred from map completion. Flag candidates for evidence
    # reconciliation without auto-marking DONE or pausing unrelated workers.
    status_drift_candidates = []
    for task in tasks:
        if task["status"] not in {"PENDING", "BUILDING", "REVIEW"} or not task.get("maps") or not task.get("coverage_complete"):
            continue
        try:
            check = _load_maps(task["maps"], project=project)
        except (ValueError, OSError):
            continue
        if check["requirements"]["done"] == check["requirements"]["total"]:
            status_drift_candidates.append(task["id"])
    queue_audit = audit_execution_queue(tasks, project=project)
    # Always distinguish total owner requests from requirements/slices.
    snap = {
        "at_local": _clock(), "scope": label, "scope_file": str(path),
        "tasks": {"done": status_count["VERIFIED"], "total": len(tasks) - status_count["CANCELLED"]},
        "task_statuses": dict(sorted(status_count.items())),
        "owner_inputs": {"registered": len(raw_inputs), "reconciled": len(raw_inputs)-len(pending_inputs),
                         "pending": pending_inputs},
        "unmapped_tasks": uncovered, "stale_sources": stale,
        "source_coverage": section_coverage, "status_drift_candidates": status_drift_candidates,
        "execution_queue": queue_audit, "queue_registration_complete": queue_audit["complete"],
        "measurable": not stale and not pending_inputs,
    }
    if pending_inputs:
        snap["intake_pending"] = pending_inputs
    if stale or pending_inputs:
        snap["reason"] = ("есть зарегистрированные, но ещё не согласованные поручения владельца: "
                          + ", ".join(pending_inputs[:8]) if pending_inputs else
                          "область задач или их подтверждения устарели; сначала сверить исходники, карты и статусы")
        return snap
    if map_paths:
        snap.update(_load_maps(map_paths, namespaces=map_namespaces, project=project))
    if uncovered:
        snap["requirements_measurable"] = False
        snap["requirements_reason"] = f"{len(uncovered)} задач ещё не перенесены полностью в карты требований"
    elif not map_paths:
        snap["requirements_measurable"] = False
        snap["requirements_reason"] = "нет карт требований"
    else:
        snap["requirements_measurable"] = True
    return snap


def _clock() -> str:
    dt = datetime.now().astimezone()
    off = dt.strftime("%z")
    if re.fullmatch(r"[+-]\d{4}", off):
        off = off[:3] + ":" + off[3:]
    return dt.strftime("%Y-%m-%d %H:%M") + f" UTC{off}" if off else dt.isoformat(timespec="minutes")


def _stat(done: int, total: int, width: int = 20) -> str:
    pct = (100 * done // total) if total else 0
    full = (width * done // total) if total else 0
    return f"{done}/{total} ({pct}%) [{('#' * full) + ('-' * (width - full))}]"


def snapshot(paths: list[str], label: str, scope_file: str | None = None,
             require_queue: bool = False) -> dict:
    if scope_file:
        snap = _load_scope(scope_file, label)
        if require_queue and not snap["queue_registration_complete"]:
            snap["measurable"] = False
            queue_reason = "очередь исполнения не завершена: отсутствует позиция, приоритет или проверяемая зависимость"
            snap["reason"] = snap.get("reason", "") + ("; " if snap.get("reason") else "") + queue_reason
        return snap
    snap = {"at_local": _clock(), "scope": label}
    if paths:
        snap.update(_load_maps(paths))
        snap["measurable"] = True
    else:
        snap["measurable"] = False
        snap["reason"] = "не указаны карты/реестр активных задач; процент нельзя подтвердить"
    return snap


def present(snap: dict) -> str:
    lines = [f"Время среды: {snap['at_local']}", f"Область: {snap['scope']}"]
    if "execution_queue" in snap and not snap["execution_queue"]["complete"]:
        lines.append("Очередь исполнения: не подтверждена — " +
                     "; ".join(snap["execution_queue"]["issues"][:5]))
    if not snap["measurable"]:
        lines.append("Прогресс: не рассчитан — " + snap["reason"])
        if snap.get("stale_sources"):
            lines.append("Причины: " + "; ".join(snap["stale_sources"][:5]))
        if snap.get("intake_pending"):
            lines.append("Не учтены входящие поручения: " + ", ".join(snap["intake_pending"][:8]))
        return "\n".join(lines)
    if "tasks" in snap:
        task = snap["tasks"]
        lines.append("Поручения владельца подтверждены: " + _stat(task["done"], task["total"]))
        if not snap.get("requirements_measurable"):
            lines.append("Общий процент по требованиям: не рассчитан — " + snap.get("requirements_reason", "данные неполные"))
            if snap.get("unmapped_tasks"):
                lines.append("Ожидают учёта в картах: " + ", ".join(snap["unmapped_tasks"][:12]))
            return "\n".join(lines)
    s, r = snap["slices"], snap["requirements"]
    lines.append("Этапы полностью подтверждены: " + _stat(s["done"], s["total"]))
    lines.append("Требования подтверждены:      " + _stat(r["done"], r["total"]))
    due = snap["statuses"].get("READY_FOR_LIVE", 0)
    blocked = snap["statuses"].get("BLOCKED_REAL_HUMAN_GATE", 0)
    failed = snap["statuses"].get("FAILED", 0)
    parts = []
    if due: parts.append(f"ждут проверки после деплоя: {due}")
    if blocked: parts.append(f"требуют решения пользователя: {blocked}")
    if failed: parts.append(f"с ошибкой: {failed}")
    if parts: lines.append("Отдельно: " + "; ".join(parts))
    optional = snap.get("owner_optional_checks", {})
    if optional.get("total"):
        lines.append(f"Необязательная обратная связь владельца: {optional['pending']} ожидают, "
                     f"{optional['verified']} просмотрены (вне знаменателя и без блокировки DONE).")
    lines.append("Источник счёта: только переданные карты; статусы не являются проверкой кода/production.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Точный счётчик прогресса по активным картам требований")
    parser.add_argument("--map", action="append", default=[], help="Путь к карте требований (устаревший выбор только карт одной задачи)")
    parser.add_argument("--scope-file", help="Проверяемый реестр всех поручений plans/progress_scope.json; нужен для общего прогресса")
    parser.add_argument("--label", default="активное ТЗ", help="Человекочитаемое название области счёта")
    parser.add_argument("--json", action="store_true", help="Отдать структурированные данные")
    parser.add_argument("--inventory-template", action="store_true", help="Вывести шаблон обратного учёта current_task, не изменяя файлы")
    parser.add_argument("--require-queue", action="store_true", help="Не подтверждать общий прогресс при незавершённой регистрации в исполняемой очереди")
    parser.add_argument("--source-file", help="Источник для шаблона (по умолчанию plans/current_task.md)")
    parser.add_argument("--compare-ref", action="append", default=[], help="Git ref/SHA проверенного релиза или интеграции (явно выбранный)")
    parser.add_argument("--compare-worktree", action="append", default=[], help="другая активная рабочая копия того же репозитория")
    args = parser.parse_args(argv)
    try:
        if args.inventory_template:
            if not args.scope_file:
                raise ValueError("--inventory-template требует --scope-file")
            scope = Path(args.scope_file).resolve()
            project = scope.parent.parent if scope.parent.name == "plans" else scope.parent
            source = _project_path(args.source_file or "plans/current_task.md", project)
            if not source.is_file():
                raise ValueError(f"нет исходного файла: {source}")
            print(json.dumps(inventory_template(source, project), ensure_ascii=False, indent=2))
            return 0
        if args.require_queue and not args.scope_file:
            raise ValueError("--require-queue требует --scope-file")
        data = snapshot(args.map, args.label, args.scope_file, require_queue=args.require_queue)
        if args.compare_ref or args.compare_worktree:
            if not args.scope_file:
                raise ValueError("сверка веток требует --scope-file")
            from workflow_lineage import lineage, LineageError
            scope_path = Path(args.scope_file).resolve()
            project = scope_path.parent.parent if scope_path.parent.name == "plans" else scope_path.parent
            comparisons = lineage(project, args.compare_ref, args.compare_worktree)
            data["lineage"] = comparisons
            data["lineage_checked"] = True
            if any(row["needs_reconciliation"] for row in comparisons):
                data["measurable"] = False
                data["reason"] = "реестры разных рабочих копий/релизов расходятся; сначала сверить lineage и доказательства"
                data.setdefault("stale_sources", []).append("обнаружен cross-branch scope drift")
        elif args.scope_file:
            data["lineage_checked"] = False
            data["certification"] = "только локальная рабочая копия; линия релиза не проверена"
    except (OSError, ValueError) as exc:
        print("ПРОГРЕСС НЕИЗВЕСТЕН: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else present(data))
    # Queue admission is an explicit gate when requested, not just a warning in
    # a wall of JSON. Exit 3 preserves diagnostic output and mutates nothing.
    return 3 if args.require_queue and not data.get("queue_registration_complete", False) else 0


if __name__ == "__main__":
    sys.exit(main())
