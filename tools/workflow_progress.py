#!/usr/bin/env python3
"""Read-only progress snapshot of explicitly selected active requirement maps.

Never infers task completion from files/tests/commits alone. Only VERIFIED requirements
count as completed; a slice is completed if every mapped requirement is VERIFIED.
Use one or more --map arguments to define the entire active reporting scope.
Run from the project repository root; maps are project files, this script is global.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import sys

SCHEMA = "openworkflow-coverage-v1"
KNOWN_STATES = {"PENDING", "BUILDING", "REVIEW", "VERIFIED", "FAILED", "BLOCKED_REAL_HUMAN_GATE", "READY_FOR_LIVE"}


def _load_maps(paths: list[str]) -> dict:
    statuses: dict[tuple[str, str], str] = {}
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    duplicates = 0
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
            key = (str(p), rid)
            if key in statuses:
                duplicates += 1
                # Contradictory duplicates are data quality defects, not two requirements.
                if statuses[key] != state:
                    raise ValueError(f"у одного требования два статуса: {rid} в {p}")
            else:
                statuses[key] = state
            owners[owner].add(key)
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
        "unverified": len(statuses) - completed_req,
        "duplicates_collapsed": duplicates,
    }


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


def snapshot(paths: list[str], label: str) -> dict:
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
    lines.append("Источник счёта: только переданные карты; статусы не являются проверкой кода/production.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Точный счётчик прогресса по активным картам требований")
    parser.add_argument("--map", action="append", default=[], help="Путь к карте требований; повторить для всех карт активного эпика")
    parser.add_argument("--label", default="активное ТЗ", help="Человекочитаемое название области счёта")
    parser.add_argument("--json", action="store_true", help="Отдать структурированные данные")
    args = parser.parse_args(argv)
    try:
        data = snapshot(args.map, args.label)
    except (OSError, ValueError) as exc:
        print("ПРОГРЕСС НЕИЗВЕСТЕН: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else present(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
