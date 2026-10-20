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
TASK_STATES = {"PENDING", "BUILDING", "REVIEW", "READY_FOR_LIVE", "WAITING_EXTERNAL", "BLOCKED_REAL_HUMAN_GATE", "FAILED", "VERIFIED"}
KNOWN_STATES = {"PENDING", "BUILDING", "REVIEW", "VERIFIED", "FAILED", "BLOCKED_REAL_HUMAN_GATE", "READY_FOR_LIVE"}


def optional_owner_check(row: dict) -> bool:
    return row.get("verification_tier", "engineering") == "owner_optional"



def _load_maps(paths: list[str], namespaces: dict[Path, str] | None = None) -> dict:
    statuses: dict[tuple[str, str], str] = {}
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    duplicates = 0
    optional_statuses = {}
    paths_seen = set()
    ordered_paths: list[str] = []
    for raw_path in paths:
        p = Path(raw_path).resolve()
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
                    policy = Path.cwd() / policy
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
    tasks = root["tasks"]
    if not tasks:
        raise ValueError("реестр задач пуст; проценты не подтверждены")
    task_ids, map_paths, uncovered, stale = set(), [], [], []
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
            source = Path(sp).resolve()
            if not source.is_file():
                stale.append(f"{tid}: исходный файл не найден ({sp})")
            elif hashlib.sha256(source.read_bytes()).hexdigest().lower() != fingerprint.lower():
                stale.append(f"{tid}: исходное задание изменилось после последнего учёта")
        mp = row.get("maps", [])
        if not isinstance(mp, list) or not all(isinstance(x, str) and x.strip() for x in mp):
            raise ValueError(f"неверный список карт для задачи {tid}")
        # New tasks count immediately; requirements count only after PM maps them.
        if not row.get("coverage_complete") or not mp:
            uncovered.append(tid)
        if status == "VERIFIED" and mp:
            completed_with_maps.append((tid, mp))
        for raw_map in mp:
            resolved = Path(raw_map).resolve()
            if resolved in map_namespaces and map_namespaces[resolved] != tid:
                raise ValueError(f"одна карта привязана к двум разным поручениям: {raw_map}")
            map_namespaces[resolved] = tid
            map_paths.append(raw_map)
    for tid, mapped_paths in completed_with_maps:
        if tid in uncovered:
            stale.append(f"{tid}: задача помечена VERIFIED без завершённого учёта требований")
            continue
        try:
            evidence = _load_maps(mapped_paths)
            if evidence["requirements"]["done"] != evidence["requirements"]["total"]:
                stale.append(f"{tid}: задача VERIFIED, но в карте остались неподтверждённые требования")
        except ValueError as exc:
            stale.append(f"{tid}: ошибочная карта завершённой задачи: {exc}")
    # Always distinguish total owner requests from requirements/slices.
    snap = {
        "at_local": _clock(), "scope": label, "scope_file": str(path),
        "tasks": {"done": status_count["VERIFIED"], "total": len(tasks)},
        "task_statuses": dict(sorted(status_count.items())),
        "unmapped_tasks": uncovered, "stale_sources": stale,
        "measurable": not stale,
    }
    if stale:
        snap["reason"] = "область задач или их подтверждения устарели; сначала сверить исходники, карты и статусы"
        return snap
    if map_paths:
        snap.update(_load_maps(map_paths, namespaces=map_namespaces))
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


def snapshot(paths: list[str], label: str, scope_file: str | None = None) -> dict:
    if scope_file:
        return _load_scope(scope_file, label)
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
    if not snap["measurable"]:
        lines.append("Прогресс: не рассчитан — " + snap["reason"])
        if snap.get("stale_sources"):
            lines.append("Причины: " + "; ".join(snap["stale_sources"][:5]))
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
    args = parser.parse_args(argv)
    try:
        data = snapshot(args.map, args.label, args.scope_file)
    except (OSError, ValueError) as exc:
        print("ПРОГРЕСС НЕИЗВЕСТЕН: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else present(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
