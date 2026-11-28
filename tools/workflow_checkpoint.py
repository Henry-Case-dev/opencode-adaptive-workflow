#!/usr/bin/env python3
"""Atomic repair of the existing human-readable workflow checkpoint (no new state).

Only repairs stale/duplicated Status and Next execution when an independently
eligible queued task exists and the current header has exactly one Human gate:
none or a valid task-local gate for unrelated work. Never changes owner inputs, task statuses or authorizes a dispatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from workflow_progress import ready_task_ids, task_index, task_status, PASSIVE_STATES
KEY_RE = re.compile(r"^(Status|Human gate|Next execution):[ \t]*(.*)$", re.I)
MACHINE_RE = re.compile(r"^([a-z][a-z0-9_-]*)(?:[ \t]*\([^()\r\n]*\))?$", re.I)


def ready_tasks(scope):
    return ready_task_ids(scope)


def split_header(text: str) -> tuple[str, str]:
    # Historical notes under ## are not machine fields.
    match = re.search(r"(?m)^##\s", text)
    if match:
        return text[:match.start()], text[match.start():]
    return text, ""


def fields(header: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in header.splitlines():
        match = KEY_RE.match(line)
        if match:
            found.setdefault(match[1].lower(), []).append(match[2].strip())
    return found


def machine_value(value: str) -> str | None:
    match = MACHINE_RE.fullmatch(value)
    return match[1].lower() if match else None


def state(scope: object, text: str) -> dict:
    header, _history = split_header(text)
    values = fields(header)
    human = values.get("human gate", [])
    ready = ready_tasks(scope)
    if len(human) != 1:
        return {"safe": False, "reason": "missing-or-duplicate-human-gate", "ready": ready}
    if machine_value(human[0]) != "none":
        match = re.fullmatch(r'task\s*\(([^()\r\n]+)\)', human[0], re.I)
        ids, _ = task_index(scope) if isinstance(scope, dict) and isinstance(scope.get('tasks'), list) else ({}, set())
        gate = match[1].strip() if match else None
        if not gate or task_status(ids.get(gate)) not in PASSIVE_STATES or not ready or gate in ready:
            return {"safe": False, "reason": "human-gate", "ready": ready}
    statuses = values.get("status", [])
    executions = values.get("next execution", [])
    # Do not fabricate a missing checkpoint header from task inventory alone.
    # Only known, potentially stale or duplicated machine values are repairable.
    if not statuses or any(machine_value(x) not in {
        "in_progress", "done", "waiting_external", "blocked"
    } for x in statuses):
        return {"safe": False, "reason": "status-missing-or-invalid", "ready": ready}
    if not executions or any(machine_value(x) not in {
        "runnable", "awaiting_children", "none"
    } for x in executions):
        return {"safe": False, "reason": "next-execution-missing-or-invalid", "ready": ready}
    if not ready:
        return {"safe": False, "reason": "no-certified-ready-queue-task", "ready": []}
    return {"safe": True, "reason": "certified-ready-handoff", "ready": ready,
            "duplicate_status": len(values.get("status", [])),
            "duplicate_next_execution": len(values.get("next execution", []))}


def repair_header(text: str) -> str:
    header, history = split_header(text)
    # Preserve other keys, comments and all history. Only replace Status / Next execution.
    lines = header.splitlines(keepends=True)
    seen = set()
    result = []
    newline = "\r\n" if "\r\n" in text else "\n"
    for line in lines:
        stripped = line.rstrip("\r\n")
        match = KEY_RE.match(stripped)
        key = match[1].lower() if match else None
        if key in {"status", "next execution"}:
            if key not in seen:
                result.append(("Status: in_progress" if key == "status" else "Next execution: runnable") + newline)
                seen.add(key)
            continue
        result.append(line)
    if result and not result[-1].endswith(("\n", "\r")):
        result[-1] += newline
    for key, value in (("status", "Status: in_progress"), ("next execution", "Next execution: runnable")):
        if key not in seen:
            result.append(value + newline)
    return "".join(result) + history


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply an atomic verified repair")
    parser.add_argument("--expected-sha256", help="Required in --apply mode; use inspected checkpoint hash")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_sha256:
        parser.error("--apply requires --expected-sha256")
    repo = args.repo.resolve()
    checkpoint = repo / "plans" / "workflow_state.md"
    scope_file = repo / "plans" / "progress_scope.json"
    lock = checkpoint.with_name(".workflow_state.md.lock")
    have_lock = False
    try:
        if args.apply:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                print(json.dumps({"safe": False, "reason": "checkpoint-write-locked"}))
                return 2
            os.close(fd)
            have_lock = True
        original = checkpoint.read_bytes()
        initial_sha = sha(original)
        if args.apply and args.expected_sha256 != initial_sha:
            print(json.dumps({"safe": False, "reason": "checkpoint-cas-mismatch", "sha256": initial_sha}))
            return 2
        scope = json.loads(scope_file.read_text(encoding="utf-8-sig"))
        content = original.decode("utf-8-sig")
        evaluation = state(scope, content)
        output = {**evaluation, "sha256": initial_sha, "changed": False}
        if not evaluation["safe"]:
            print(json.dumps(output, ensure_ascii=False))
            return 2 if args.apply else 0
        updated = repair_header(content)
        output["changed"] = updated != content
        if args.apply and output["changed"]:
            # Compare-after-read under the cooperative lock. Never clobber
            # another tool's newer state or any concurrent scope update.
            scope_before = scope_file.read_bytes()
            if checkpoint.read_bytes() != original:
                print(json.dumps({"safe": False, "reason": "checkpoint-changed-during-repair"}))
                return 2
            if json.loads(scope_before.decode("utf-8-sig")) != scope:
                print(json.dumps({"safe": False, "reason": "scope-changed-during-repair"}))
                return 2
            temp = None
            try:
                with tempfile.NamedTemporaryFile("wb", dir=checkpoint.parent,
                                                 prefix=".workflow_state.", suffix=".tmp", delete=False) as file:
                    temp = Path(file.name)
                    file.write(updated.encode("utf-8"))
                    file.flush()
                    os.fsync(file.fileno())
                if checkpoint.read_bytes() != original or scope_file.read_bytes() != scope_before:
                    print(json.dumps({"safe": False, "reason": "concurrent-source-change"}))
                    return 2
                os.replace(temp, checkpoint)
                temp = None
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
        output["applied"] = bool(args.apply)
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"safe": False, "reason": "read-or-write-error", "message": str(exc)[:200]}, ensure_ascii=False))
        return 2
    finally:
        if have_lock:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
