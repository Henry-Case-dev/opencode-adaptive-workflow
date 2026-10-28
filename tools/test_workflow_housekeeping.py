"""Offline regression tests: no external services, no production access."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).with_name('workflow_housekeeping.py')

class HygieneTests(unittest.TestCase):
    def setUp(self):
        test_root = SOURCE.resolve().parents[1] / 'WorkspaceData' / 'temp' / 'tests'
        test_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=test_root)
        self.dir = Path(self.tmp.name)
        self.config = self.dir/'config'
        tools = self.config/'tools'; tools.mkdir(parents=True)
        self.script=tools/'workflow_housekeeping.py'
        shutil.copy2(SOURCE,self.script)
        self.repo=self.dir/'source'/'example';self.repo.mkdir(parents=True)
        self.git('-C',str(self.repo),'init','-q')
        self.git('-C',str(self.repo),'config','user.email','test@example.invalid')
        self.git('-C',str(self.repo),'config','user.name','test')
        (self.repo/'README.md').write_text('base\n')
        self.git('-C',str(self.repo),'add','.')
        self.git('-C',str(self.repo),'commit','-qm','base')
        self.workspace=self.config/'WorkspaceData'

    def tearDown(self):
        self.tmp.cleanup()

    def git(self,*args,check=True):
        return subprocess.run(['git',*args], text=True, capture_output=True,check=check)

    def cli(self,*args,ok=True):
        r=subprocess.run([sys.executable,str(self.script),*map(str,args)],capture_output=True,text=True)
        if ok and r.returncode:
            self.fail(f'cli failed: {r.stderr}\n{r.stdout}')
        return r

    def test_workspace_path_portable(self):
        self.cli('init')
        self.assertTrue((self.workspace/'temp').exists())
        self.assertTrue((self.workspace/'worktrees').exists())
        self.assertFalse((self.repo.parent/'WorkspaceData').exists())

    def test_epic_deletes_only_owned_temp(self):
        r=self.cli('allocate','--repo',self.repo,'--epic','one','--kind','probe')
        folder=Path(r.stdout.strip());(folder/'scratch.txt').write_text('tmp')
        self.cli('epic-close','--repo',self.repo,'--epic','one')
        self.assertTrue(folder.exists()) # dry run
        self.cli('epic-close','--repo',self.repo,'--epic','one','--confirmed-complete','--confirmed-idle','--apply')
        self.assertFalse(folder.exists())

    def test_prevents_unsafe_apply(self):
        r=self.cli('epic-close','--repo',self.repo,'--epic','one','--apply',ok=False)
        self.assertNotEqual(0,r.returncode)
        self.assertIn('requires',r.stderr)

    def test_protected_temp_retained(self):
        folder=Path(self.cli('allocate','--repo',self.repo,'--epic','protect','--kind','probe').stdout.strip())
        (folder/'database.sqlite').write_text('important')
        self.cli('epic-close','--repo',self.repo,'--epic','protect','--confirmed-complete','--confirmed-idle','--apply')
        self.assertTrue(folder.exists())

    def test_unowned_temp_retained(self):
        folder=self.workspace/'temp'/'other';folder.mkdir(parents=True)
        (folder/'a.txt').write_text('user file')
        self.cli('epic-close','--repo',self.repo,'--epic','foo','--confirmed-complete','--confirmed-idle','--apply')
        self.assertTrue(folder.exists())

    def setup_worktree(self,epic):
        wt=Path(self.cli('worktree-path','--repo',self.repo,'--epic',epic).stdout.strip())
        wt.parent.mkdir(parents=True,exist_ok=True)
        self.git('-C',str(self.repo),'worktree','add','-q','-b','test'+epic,str(wt))
        self.cli('register-worktree','--repo',self.repo,'--epic',epic,'--path',wt)
        return wt

    def test_clean_integrated_worktree_retired(self):
        wt=self.setup_worktree('good')
        self.cli('epic-close','--repo',self.repo,'--epic','good','--confirmed-complete','--confirmed-idle','--apply')
        self.assertFalse(wt.exists())

    def test_dirty_worktree_retained(self):
        wt=self.setup_worktree('dirty')
        (wt/'file.txt').write_text('work in progress')
        self.cli('epic-close','--repo',self.repo,'--epic','dirty','--confirmed-complete','--confirmed-idle','--apply')
        self.assertTrue(wt.exists())

    def test_unique_commits_worktree_retained(self):
        wt=self.setup_worktree('unique')
        (wt/'README.md').write_text('unique')
        self.git('-C',str(wt),'add','.')
        self.git('-C',str(wt),'commit','-qm','unique')
        self.cli('epic-close','--repo',self.repo,'--epic','unique','--confirmed-complete','--confirmed-idle','--apply')
        self.assertTrue(wt.exists())

    def test_unregistered_legacy_worktree_untouched(self):
        outside=self.repo.parent/'old-wt';self.git('-C',str(self.repo),'worktree','add','-q','-b','old',str(outside))
        self.cli('epic-close','--repo',self.repo,'--epic','old','--confirmed-complete','--confirmed-idle','--apply')
        self.assertTrue(outside.exists())

    def test_owned_index_cache_can_be_purged(self):
        cache=self.workspace/'cache'/'intelligence'/'index';cache.mkdir(parents=True)
        (cache/'.opencode-repo-cache.json').write_text(json.dumps({'schema':1,'repo':str(self.repo.resolve()),'managedBy':'opencode-global-intelligence-v3'}))
        (cache/'repo-intel.sqlite3').write_text('disposable')
        self.cli('epic-close','--repo',self.repo,'--epic','cache','--purge-disposable-cache','--confirmed-complete','--confirmed-idle','--apply')
        self.assertFalse(cache.exists())

    def test_unknown_index_content_retained(self):
        cache=self.workspace/'cache'/'intelligence'/'unknown';cache.mkdir(parents=True)
        (cache/'.opencode-repo-cache.json').write_text(json.dumps({'schema':1,'repo':str(self.repo.resolve()),'managedBy':'opencode-global-intelligence-v3'}))
        (cache/'custom.db').write_text('not disposable')
        self.cli('epic-close','--repo',self.repo,'--epic','cache','--purge-disposable-cache','--confirmed-complete','--confirmed-idle','--apply')
        self.assertTrue(cache.exists())

if __name__=='__main__':unittest.main()
