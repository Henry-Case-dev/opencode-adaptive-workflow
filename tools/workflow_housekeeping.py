#!/usr/bin/env python3
"""Project-portable OpenCode workspace hygiene. Safe by default; no production access.

Managed scratch and worktrees live below the OpenCode config containing this script:
  <config>/WorkspaceData/
Legacy/unowned locations are REPORT ONLY and never deleted automatically.
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
ROOT = CONFIG / 'WorkspaceData'
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
    for name in ['temp', 'worktrees', 'cache', 'reports', 'quarantine', 'registry/worktrees']:
        (ROOT/name).mkdir(parents=True, exist_ok=True)
    print('Workspace:', ROOT)


def allocate(args):
    repo = canonical(args.repo)
    if not (repo/'.git').exists():
        raise RuntimeError('Not a Git project root')
    path = ROOT/'temp'/project_id(repo)/epic_id(args.epic)/(args.kind + '-' + secrets.token_hex(5))
    path.mkdir(parents=True, exist_ok=False)
    atomic_json(path/MARK, {'schema': 2, 'kind': args.kind, 'repo': str(repo), 'epic': args.epic})
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
    record={'schema': 2, 'repo': str(repo), 'path': str(wt), 'epic': args.epic}
    atomic_json(registry_file(wt),record)
    print('Registered:',wt)


def inspect_worktree(repo, wt):
    registered=listed_worktrees(repo)
    if str(wt) not in registered: return 'not registered with Git'
    if wt.is_symlink() or not is_inside(wt,ROOT/'worktrees'): return 'unsafe path'
    status=git(wt,'status','--porcelain=v1','--untracked-files=all',check=True)
    if status.stdout.strip(): return 'uncommitted or untracked files'
    head=git(wt,'rev-parse','HEAD',check=True).stdout.strip()
    if git(repo,'merge-base','--is-ancestor',head,'HEAD').returncode:
        return 'unique/unmerged commits (not ancestor of main working HEAD)'
    return None


def epic_close(args):
    repo=canonical(args.repo)
    if args.apply and not (args.confirmed_complete and args.confirmed_idle):
        raise RuntimeError('--apply requires --confirmed-complete AND --confirmed-idle')
    epic_path=ROOT/'temp'/project_id(repo)/epic_id(args.epic)
    deleted=skipped=0
    if epic_path.exists():
        for folder in sorted(epic_path.iterdir()):
            if not is_inside(folder, ROOT/'temp') or folder.is_symlink():
                print('SKIP unsafe:',folder); skipped+=1; continue
            marker=read_marker(folder)
            if (not marker or marker.get('schema')!=2 or marker.get('epic')!=args.epic or
                marker.get('repo')!=str(repo) or marker.get('kind') not in SAFE_KINDS or not managed_safe(folder)):
                print('SKIP protected or unowned:', folder); skipped+=1; continue
            if args.apply:
                shutil.rmtree(folder)
                print('DELETED managed temporary run:',folder)
                deleted+=1
            else:
                print('WOULD DELETE managed temporary run:',folder)
        if args.apply and not any(epic_path.iterdir()):epic_path.rmdir()
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
                # git worktree remove refuses dirty paths. Never use --force.
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
    a=sub.add_parser('allocate');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.add_argument('--kind',choices=sorted(SAFE_KINDS),default='temp');a.set_defaults(action=allocate)
    a=sub.add_parser('worktree-path');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.set_defaults(action=worktree_path)
    a=sub.add_parser('register-worktree');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.add_argument('--path',required=True);a.set_defaults(action=register_worktree)
    a=sub.add_parser('epic-close');a.add_argument('--repo',required=True);a.add_argument('--epic',required=True);a.add_argument('--apply',action='store_true');a.add_argument('--confirmed-complete',action='store_true');a.add_argument('--confirmed-idle',action='store_true');a.add_argument('--purge-disposable-cache',action='store_true');a.set_defaults(action=epic_close)
    a=sub.add_parser('audit');a.add_argument('--repo',required=True);a.set_defaults(action=audit)
    args=ap.parse_args()
    try:args.action(args)
    except (OSError,RuntimeError,ValueError) as exc:
        ap.exit(2, f'ERROR: {exc}\n')

if __name__=='__main__':main()
