#!/usr/bin/env python3
"""Project-portable OpenCode workspace hygiene. Safe by default; no production access.

Canonical root is the global OpenCode config that contains this file, plus WorkspaceData/.
Override only with OPENCODE_WORKSPACE_DATA. Do not hardcode a project path.
Layout: temp/, cache/, worktrees/, runs/, backups/, registry/.

allocate writes under WorkspaceData/runs/. worktree-path prints a path under
WorkspaceData/worktrees/. OpenCodeScratch and OpenCodeCache are legacy cleanup
targets, not write destinations. Do not copy old scratch into WorkspaceData.

Every agent finalizes its own disposable run immediately after the check that
created it (run-close); the owning agent/Orchestrator cleans a completed task
or feature (unit-close), without waiting for the epic. Both commands are dry-run
unless explicitly applied after evidence preservation and verified idle use.
Orchestrator/DevOps also run epic-close after an epic is integrated and idle.
epic-close retires only that epic's registered worktrees when
they are clean, integrated into the repo HEAD, not the main worktree, and not
in use. It never passes --force and never deletes branch refs. Unique detached
commits and dirty trees are kept. It prunes only closed tool-owned runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

CONFIG = Path(__file__).resolve().parent.parent


def workspace_root():
    override = os.environ.get('OPENCODE_WORKSPACE_DATA')
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / 'WorkspaceData'


ROOT = workspace_root()
MARK = '.opencode-workflow-owned.json'
SAFE_KINDS = {'probe', 'temp', 'report', 'cache', 'artifact'}
PROTECTED_NAMES = {'.git', '.hg', '.svn', 'plans', 'backup', 'backups', '.venv', 'venv', 'node_modules'}
PROTECTED_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.wal', '.bak', '.pem', '.key', '.pfx'}


def canonical(path):
    return Path(path).expanduser().resolve()


def is_inside(p, root):
    try:
        return p.resolve().is_relative_to(root.resolve()) and p.resolve() != root.resolve()
    except OSError:
        return False


def project_id(path):
    value = str(canonical(path))
    if os.name == 'nt':
        value = os.path.normcase(value)
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def epic_id(name):
    # Stable identity. No dependence on task's natural-language phrasing or project name.
    h = hashlib.sha256(name.encode('utf8')).hexdigest()[:12]
    title = re.sub(r'[^A-Za-z0-9_-]+', '-', name).strip('-')[:30] or 'epic'
    return f'{title}-{h}'


def git(repo, *args, check=False):
    run = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                         encoding='utf-8', errors='replace')
    if check and run.returncode:
        raise RuntimeError(f'git {args[0]} failed (exit {run.returncode}): {run.stderr.strip()[:300]}')
    return run


def read_marker(path):
    try:
        return json.loads((path / MARK).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return None


def atomic_json(file, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + '.' + secrets.token_hex(5) + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    tmp.replace(file)


def managed_safe(path):
    if path.is_symlink() or not path.is_dir():
        return False
    for parent, dirs, files in os.walk(path, followlinks=False):
        if any(Path(parent, x).is_symlink() for x in dirs + files):
            return False
        if any(x.lower() in PROTECTED_NAMES for x in dirs + files):
            return False
        if any(Path(x).suffix.lower() in PROTECTED_SUFFIXES for x in files):
            return False
    return True


def init(args):
    for name in ['temp', 'cache', 'worktrees', 'runs', 'backups', 'registry', 'registry/worktrees', 'reports', 'quarantine']:
        (ROOT/name).mkdir(parents=True, exist_ok=True)
    print('Workspace:', ROOT)


def allocate(args):
    repo = canonical(args.repo)
    if not (repo/'.git').exists():
        raise RuntimeError('Not a Git project root')
    path = ROOT/'runs'/project_id(repo)/epic_id(args.epic)/(args.kind + '-' + secrets.token_hex(5))
    path.mkdir(parents=True, exist_ok=False)
    atomic_json(path/MARK, {'schema': 2, 'kind': args.kind, 'repo': str(repo), 'epic': args.epic,
                            **({'unit': args.unit} if args.unit else {})})
    print(path)


def worktree_path(args):
    repo = canonical(args.repo)
    if not (repo/'.git').exists(): raise RuntimeError('Not a Git project root')
    target = ROOT/'worktrees'/project_id(repo)/(epic_id(args.epic)+'-'+secrets.token_hex(4))
    print(target)
    print('Create with git worktree add, then register with register-worktree.', file=sys.stderr)


def registry_file(path):
    key = hashlib.sha256(str(canonical(path)).encode()).hexdigest()
    return ROOT/'registry'/'worktrees'/(key+'.json')


def listed_worktrees(repo):
    run=git(repo,'worktree','list','--porcelain',check=True)
    result=set()
    for line in run.stdout.splitlines():
        if line.startswith('worktree '): result.add(str(canonical(line[9:])))
    return result


def register_worktree(args):
    repo=canonical(args.repo); wt=canonical(args.path)
    if not is_inside(wt,ROOT/'worktrees') or wt.is_symlink():
        raise RuntimeError('Worktree must be under WorkspaceData/worktrees')
    if str(wt) not in listed_worktrees(repo):
        raise RuntimeError('Not registered by git worktree list')
    record={'schema': 2, 'repo': str(repo), 'path': str(wt), 'epic': args.epic,
            **({'unit': args.unit} if args.unit else {})}
    atomic_json(registry_file(wt),record)
    print('Registered:',wt)


def process_uses(path):
    """Best-effort: True if a readable process cwd or command line uses path."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        ntdll = ctypes.WinDLL('ntdll')
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        kernel32.ReadProcessMemory.restype = wintypes.BOOL

        class PBI(ctypes.Structure):
            _fields_ = [('Reserved1', ctypes.c_void_p), ('PebBaseAddress', ctypes.c_void_p),
                        ('Reserved2', ctypes.c_void_p * 2), ('UniqueProcessId', ctypes.c_void_p),
                        ('Reserved3', ctypes.c_void_p)]

        def cwd_of(pid):
            h = kernel32.OpenProcess(0x0410, False, pid)
            if not h:
                return ''
            try:
                pbi = PBI(); ret = ctypes.c_ulong()
                if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret)) or not pbi.PebBaseAddress:
                    return ''
                params = ctypes.c_void_p(); read = ctypes.c_size_t()
                if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(pbi.PebBaseAddress + 0x20), ctypes.byref(params), ctypes.sizeof(params), ctypes.byref(read)) or not params.value:
                    return ''
                us = (ctypes.c_ubyte * 16)()
                if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(params.value + 0x38), ctypes.byref(us), 16, ctypes.byref(read)):
                    return ''
                length = int.from_bytes(bytes(us[:2]), 'little')
                buf_ptr = int.from_bytes(bytes(us[8:16]), 'little')
                if length <= 0 or length > 32768 or not buf_ptr:
                    return ''
                buf = (ctypes.c_ubyte * length)()
                if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(buf_ptr), ctypes.byref(buf), length, ctypes.byref(read)):
                    return ''
                return bytes(buf).decode('utf-16-le', 'replace').rstrip('\x00')
            finally:
                kernel32.CloseHandle(h)

        run = subprocess.run(['powershell', '-NoProfile', '-Command',
                              'Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'],
                             capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        data = json.loads(run.stdout) if run.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        needle = os.path.normcase(str(path))
        for item in data:
            pid = int(item.get('ProcessId') or 0)
            cmd = item.get('CommandLine') or ''
            if needle in os.path.normcase(cmd):
                return True
            cwd = cwd_of(pid) if pid else ''
            if cwd:
                cwd_n = os.path.normcase(cwd)
                if cwd_n == needle or cwd_n.startswith(needle + os.sep):
                    return True
        return False
    except Exception:
        return False


def inspect_worktree(repo, wt):
    registered=listed_worktrees(repo)
    if canonical(wt) == canonical(repo): return 'main worktree'
    if str(wt) not in registered: return 'not registered with Git'
    if wt.is_symlink() or not is_inside(wt,ROOT/'worktrees'): return 'unsafe path'
    if process_uses(wt): return 'active process is using this directory'
    status=git(wt,'status','--porcelain=v1','--untracked-files=all',check=True)
    if status.stdout.strip(): return 'uncommitted or untracked files'
    head=git(wt,'rev-parse','HEAD',check=True).stdout.strip()
    symbolic=git(wt,'symbolic-ref','-q','HEAD')
    if symbolic.returncode and git(repo,'merge-base','--is-ancestor',head,'HEAD').returncode:
        return 'detached HEAD not contained in the integrated branch'
    if git(repo,'merge-base','--is-ancestor',head,'HEAD').returncode:
        return 'unique/unmerged commits (not ancestor of main working HEAD)'
    return None


def run_reason(repo, epic, folder, unit=None):
    """Only delete a positively identified, tool-owned disposable directory."""
    repo = canonical(repo)
    folder = Path(folder)
    valid_parents = [ROOT / part / project_id(repo) / epic_id(epic) for part in ('runs', 'temp')]
    # Use lexical parent check as well as resolved containment: a symlink anywhere
    # in the parent chain must never redirect cleanup outside managed storage.
    if folder.is_symlink() or not any(folder.parent == parent for parent in valid_parents):
        return 'outside the exact managed run scope or symlink'
    if not folder.is_dir() or not any(is_inside(folder, parent) for parent in valid_parents):
        return 'missing, redirected or unsafe path'
    marker = read_marker(folder)
    if (not isinstance(marker, dict) or marker.get('schema') != 2 or
        marker.get('repo') != str(repo) or marker.get('epic') != epic or
        marker.get('kind') not in SAFE_KINDS):
        return 'unowned, wrong project, or invalid marker'
    if unit is not None and marker.get('unit') != unit:
        return 'belongs to a different unit (or has no unit tag)'
    if not managed_safe(folder):
        return 'contains protected or unknown content'
    return None


def dispose_run(repo, epic, folder, apply, unit=None):
    reason = run_reason(repo, epic, folder, unit)
    if reason:
        print('KEEP managed run:', folder, '-', reason)
        return 0, 1
    if apply:
        try:
            shutil.rmtree(folder)
            print('DELETED completed disposable run:', folder)
            return 1, 0
        except OSError as exc:
            print('KEEP run (locked/error):', folder, '-', exc)
            return 0, 1
    print('WOULD DELETE completed disposable run:', folder)
    return 0, 0


def verified_cleanup_flags(args):
    if args.apply and not (args.confirmed_idle and args.confirmed_evidence):
        raise RuntimeError('--apply requires --confirmed-idle AND --confirmed-evidence')


def run_close(args):
    """Close one disposable check/probe after proof has been promoted."""
    verified_cleanup_flags(args)
    repo = canonical(args.repo)
    folder = Path(args.path).expanduser()
    # Never rely on an arbitrary caller-supplied label or folder marker alone.
    deleted, skipped = dispose_run(repo, args.epic, folder, args.apply)
    print(f'Run close: removed={deleted}; retained={skipped}; mode=' + ('APPLY' if args.apply else 'DRY-RUN'))


def unit_close(args):
    """Close one finished task/feature, keeping other units and shared epic data."""
    verified_cleanup_flags(args)
    repo = canonical(args.repo)
    deleted = skipped = 0
    for part in ('runs', 'temp'):
        epic_path = ROOT / part / project_id(repo) / epic_id(args.epic)
        if not epic_path.is_dir() or epic_path.is_symlink():
            continue
        for folder in sorted(epic_path.iterdir()):
            marker = read_marker(folder) if folder.is_dir() and not folder.is_symlink() else None
            # Untagged legacy runs and other units are not candidates for a unit close.
            if not isinstance(marker, dict) or marker.get('unit') != args.unit:
                continue
            d, k = dispose_run(repo, args.epic, folder, args.apply, unit=args.unit)
            deleted += d; skipped += k
        if args.apply and epic_path.exists() and not any(epic_path.iterdir()):
            epic_path.rmdir()
    registry = ROOT/'registry'/'worktrees'
    if registry.exists():
        for file in sorted(registry.glob('*.json')):
            try: record = json.loads(file.read_text(encoding='utf8'))
            except (OSError, ValueError): continue
            if (record.get('schema') != 2 or record.get('repo') != str(repo) or
                record.get('epic') != args.epic or record.get('unit') != args.unit):
                continue
            wt = canonical(record.get('path', ''))
            try: reason = inspect_worktree(repo, wt)
            except RuntimeError as exc: reason = str(exc)
            if reason:
                print('KEEP worktree:', wt, '-', reason); skipped += 1; continue
            if args.apply:
                result = git(repo, 'worktree', 'remove', str(wt))
                if result.returncode:
                    print('KEEP worktree (git refused):', wt, result.stderr.strip())
                    skipped += 1; continue
                file.unlink()
                print('REMOVED integrated task worktree:', wt)
                deleted += 1
            else:
                print('WOULD REMOVE integrated task worktree:', wt)
    print(f'Unit close: removed={deleted}; retained={skipped}; mode=' + ('APPLY' if args.apply else 'DRY-RUN'))


def shared_scratch_close(args):
    """Remove plugin-owned *temporary* shared areas only when all project consumers are idle.

    Intentionally leaves repo-intelligence SQLite indexes and package caches alone:
    those can be useful, expensive to rebuild, and may be used by other sessions.
    """
    if args.apply and not (args.confirmed_project_idle and args.confirmed_evidence):
        raise RuntimeError('--apply requires --confirmed-project-idle AND --confirmed-evidence')
    repo = canonical(args.repo)
    import hashlib as _hashlib
    key = repo.name + '-' + _hashlib.sha1(str(repo).encode('utf8')).hexdigest()[:12]
    base = ROOT/'temp'/'intelligence'
    folder = base/key
    if folder.is_symlink() or not folder.is_dir() or not is_inside(folder, base):
        print('KEEP shared scratch: no safe managed folder')
        return
    try: proof = json.loads((folder/'.opencode-workflow-scratch.json').read_text(encoding='utf8'))
    except (ValueError, OSError):
        print('KEEP shared scratch: ownership unknown')
        return
    if proof.get('managedBy') != 'opencode-global-intelligence-v3' or str(canonical(proof.get('repo', ''))) != str(repo):
        print('KEEP shared scratch: ownership mismatched')
        return
    removed = skipped = 0
    for name in ('tmp', 'lanes', 'pycache'):
        sub = folder/name
        if not sub.exists() and not sub.is_symlink(): continue
        if not is_inside(sub, folder) or not managed_safe(sub):
            print('KEEP shared scratch child:', sub, '- protected or unsafe')
            skipped += 1;continue
        if args.apply:
            try: shutil.rmtree(sub);removed += 1;print('DELETED idle shared scratch:',sub)
            except OSError as exc: skipped += 1;print('KEEP locked scratch:',sub,exc)
        else:
            print('WOULD DELETE idle shared scratch:',sub)
    print(f'Shared scratch: removed={removed}; retained={skipped}; mode='+('APPLY' if args.apply else 'DRY-RUN'))


def epic_close(args):
    repo=canonical(args.repo)
    if args.apply and not (args.confirmed_complete and args.confirmed_idle):
        raise RuntimeError('--apply requires --confirmed-complete AND --confirmed-idle')
    deleted=skipped=0
    # Closed tool-owned runs only. Legacy allocations may still sit under temp/.
    for parent in (ROOT/'runs', ROOT/'temp'):
        epic_path=parent/project_id(repo)/epic_id(args.epic)
        if not epic_path.exists():
            continue
        for folder in sorted(epic_path.iterdir()):
            if not is_inside(folder, parent) or folder.is_symlink():
                print('SKIP unsafe:',folder); skipped+=1; continue
            d, k = dispose_run(repo, args.epic, folder, args.apply)
            deleted += d; skipped += k
        if args.apply and epic_path.exists() and not any(epic_path.iterdir()):
            epic_path.rmdir()
    registry=ROOT/'registry'/'worktrees'
    if registry.exists():
        for file in sorted(registry.glob('*.json')):
            try: record=json.loads(file.read_text(encoding='utf8'))
            except (ValueError,OSError): continue
            if record.get('schema')!=2 or record.get('repo')!=str(repo) or record.get('epic')!=args.epic:continue
            wt=canonical(record.get('path',''))
            try: reason=inspect_worktree(repo,wt)
            except RuntimeError as e: reason=str(e)
            if reason:
                print('KEEP worktree:',wt,'-',reason); skipped+=1;continue
            if args.apply:
                # Never --force. Never delete the branch ref. Dirty/unique paths stay.
                result=git(repo,'worktree','remove',str(wt))
                if result.returncode:
                    print('KEEP worktree (git refused):',wt, result.stderr.strip()); skipped+=1;continue
                file.unlink()
                print('REMOVED clean integrated worktree:',wt);deleted+=1
            else:
                print('WOULD REMOVE clean integrated worktree:',wt)
    if args.purge_disposable_cache:
        # Keep this separate from core OpenCode/OS cache. Only plugin-owned repo-index is eligible.
        scratch=ROOT/'temp'/'intelligence'
        if scratch.exists():
            for folder in scratch.iterdir():
                m=folder/'.opencode-workflow-scratch.json'
                try: proof=json.loads(m.read_text(encoding='utf8'))
                except (OSError,ValueError):continue
                if str(canonical(proof.get('repo',''))) != str(repo) or proof.get('managedBy')!='opencode-global-intelligence-v3':continue
                if not is_inside(folder,scratch) or not managed_safe(folder):
                    print('KEEP intelligence scratch (protected):',folder);skipped+=1;continue
                if args.apply:shutil.rmtree(folder);deleted+=1;print('DELETED intelligence scratch:',folder)
                else:print('WOULD DELETE intelligence scratch:',folder)
        cache=ROOT/'cache'/'intelligence'
        if cache.exists():
            for folder in cache.iterdir():
                if not folder.is_dir() or folder.is_symlink() or not is_inside(folder,cache): continue
                marker=folder/'.opencode-repo-cache.json'
                try: proof=json.loads(marker.read_text(encoding='utf8'))
                except (ValueError,OSError): continue
                if proof.get('schema')!=1 or proof.get('managedBy')!='opencode-global-intelligence-v3' or str(canonical(proof.get('repo',''))) != str(repo):continue
                known={'.opencode-repo-cache.json','repo-intel.sqlite3','repo-intel.sqlite3-wal','repo-intel.sqlite3-shm'}
                if any(p.name not in known or p.is_symlink() or p.is_dir() for p in folder.iterdir()):
                    print('KEEP index cache (unknown contents):',folder);skipped+=1;continue
                if args.apply:
                    try:shutil.rmtree(folder);deleted+=1;print('DELETED disposable repo index:',folder)
                    except OSError as ex:print('KEEP index cache (in use or locked):',folder,ex);skipped+=1
                else:print('WOULD DELETE disposable repo index:',folder)
    print(f'Done. Removed: {deleted}. Protected/skipped: {skipped}. Mode:', 'APPLY' if args.apply else 'DRY-RUN')


def audit(args):
    repo=canonical(args.repo)
    print('Project:',repo)
    print('Allowed global workflow data:',ROOT)
    print('PRODUCTION: not accessed; all operations local.')
    print('Git worktrees (including paths outside the allowed root):')
    for p in sorted(listed_worktrees(repo)):
        print(' ',p, '(PRIMARY)' if canonical(p)==repo else ('(MANAGED-AREA)' if is_inside(Path(p),ROOT) else '(LEGACY-OUTSIDE)'))
    print('Sibling folders (inventory ONLY; names are not proof of disposability):')
    for p in sorted(repo.parent.iterdir()):
        if not p.is_dir() or p==repo:continue
        print(' ',p)
    print('Legacy OpenCode cache/scratch roots (inventory ONLY):')
    for name in ['OpenCodeCache','OpenCodeScratch']:
        p=CONFIG/name
        if p.exists():print(' ',p)
    print('No deletion is performed by audit.')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest='command',required=True)
    a=sub.add_parser('init');a.set_defaults(action=init)
    a=sub.add_parser('allocate');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.add_argument('--unit');a.add_argument('--kind',choices=sorted(SAFE_KINDS),default='temp');a.set_defaults(action=allocate)
    a=sub.add_parser('worktree-path');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.set_defaults(action=worktree_path)
    a=sub.add_parser('register-worktree');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.add_argument('--unit');a.add_argument('--path',required=True);a.set_defaults(action=register_worktree)
    for command, action in (('run-close', run_close), ('unit-close', unit_close)):
        a=sub.add_parser(command)
        a.add_argument('--repo',required=True)
        a.add_argument('--epic',required=True)
        if command == 'run-close': a.add_argument('--path',required=True)
        else: a.add_argument('--unit',required=True)
        a.add_argument('--confirmed-idle',action='store_true')
        a.add_argument('--confirmed-evidence',action='store_true')
        a.add_argument('--apply',action='store_true')
        a.set_defaults(action=action)
    a=sub.add_parser('shared-scratch-close')
    a.add_argument('--repo',required=True)
    a.add_argument('--confirmed-project-idle',action='store_true')
    a.add_argument('--confirmed-evidence',action='store_true')
    a.add_argument('--apply',action='store_true')
    a.set_defaults(action=shared_scratch_close)
    a=sub.add_parser('epic-close');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.add_argument('--apply',action='store_true');a.add_argument('--confirmed-complete',action='store_true');a.add_argument('--confirmed-idle',action='store_true');a.add_argument('--purge-disposable-cache',action='store_true');a.set_defaults(action=epic_close)
    a=sub.add_parser('audit');a.add_argument('--repo',required=True);a.set_defaults(action=audit)
    args=ap.parse_args()
    try:args.action(args)
    except (OSError,RuntimeError,ValueError) as exc:
        ap.exit(2, f'ERROR: {exc}\n')

if __name__=='__main__':main()
