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

    def test_inventory_repo_compact_shows_owned_even_when_disk_has_space(self):
        run = Path(self.cli('allocate', '--repo', self.repo, '--epic', 'epic-a',
                            '--unit', 'unit-a', '--kind', 'probe').stdout.strip())
        (run / 'scratch.txt').write_text('disposable once check done')
        unowned = self.workspace / 'old_agent_probe.py'
        unowned.parent.mkdir(parents=True, exist_ok=True)
        unowned.write_text('unknown provenance')
        other = self.dir / 'source' / 'other'
        other.mkdir()
        self.git('-C', str(other), 'init', '-q')
        foreign = Path(self.cli('allocate','--repo',other,'--epic','other',
                                '--unit','foreign','--kind','probe').stdout.strip())
        result = json.loads(self.cli('inventory','--repo',self.repo).stdout)
        self.assertEqual(result['cleanup_policy'], 'lifecycle_based_not_disk_capacity')
        self.assertGreater(result['free_bytes'], 0)
        self.assertEqual(result['owned_count'], 1)
        self.assertEqual(result['ownership_groups'][0]['unit'], 'unit-a')
        self.assertIn(str(unowned), [x['path'] for x in result['unattributed_examples']])
        self.assertNotIn('owned_paths',result)  # concise by default
        self.assertTrue(run.exists())          # inventory never deletes
        self.assertTrue(unowned.exists())
        self.assertTrue(foreign.exists())
        full = json.loads(self.cli('inventory','--repo',self.repo,'--all-paths').stdout)
        self.assertEqual([x['path'] for x in full['owned_paths']], [str(run)])

    def test_inventory_read_only_never_deletes_even_with_ample_free_space(self):
        run=Path(self.cli('allocate','--repo',self.repo,'--epic','proof',
                          '--unit','u','--kind','probe').stdout.strip())
        (run/'needs-review.sqlite').write_text('protected evidence')
        result=json.loads(self.cli('inventory','--repo',self.repo).stdout)
        self.assertEqual(result['owned_count'],1)
        self.assertTrue((run/'needs-review.sqlite').exists())
        # Lifecycle close still enforces protected data even if a caller
        # claims consumer/evidence checks completed.
        close=self.cli('unit-close','--repo',self.repo,'--epic','proof',
                       '--unit','u','--apply','--confirmed-idle',
                       '--confirmed-evidence',ok=False)
        self.assertNotEqual(close.returncode,0)
        self.assertTrue((run/'needs-review.sqlite').exists())

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

    def test_run_close_after_individual_check_before_epic_end(self):
        one=Path(self.cli('allocate','--repo',self.repo,'--epic','big','--unit','test-a',
                          '--kind','probe').stdout.strip())
        two=Path(self.cli('allocate','--repo',self.repo,'--epic','big','--unit','test-b',
                          '--kind','probe').stdout.strip())
        (one/'transient.png').write_text('captured and promoted')
        (two/'still-running.txt').write_text('active')
        self.cli('run-close','--repo',self.repo,'--epic','big','--path',one)
        self.assertTrue(one.exists())  # dry run
        self.cli('run-close','--repo',self.repo,'--epic','big','--path',one,
                 '--apply','--confirmed-idle','--confirmed-evidence')
        self.assertFalse(one.exists())
        self.assertTrue(two.exists())

    def test_run_close_never_follows_nested_symlinks(self):
        run=Path(self.cli('allocate','--repo',self.repo,'--epic','safe','--unit','probe',
                          '--kind','probe').stdout.strip())
        precious=self.dir/'precious';precious.mkdir()
        (precious/'user.txt').write_text('unique')
        try:
            (run/'unsafe-link').symlink_to(precious, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink unavailable')
        result=self.cli('run-close','--repo',self.repo,'--epic','safe','--path',run,
                        '--apply','--confirmed-idle','--confirmed-evidence',ok=False)
        self.assertEqual(3,result.returncode)
        self.assertIn('KEEP',result.stdout)
        self.assertTrue(run.exists())
        self.assertTrue((precious/'user.txt').exists())

    def test_run_close_requires_both_safety_attestations(self):
        one=Path(self.cli('allocate','--repo',self.repo,'--epic','big','--kind','temp').stdout.strip())
        for args in [(),('--confirmed-idle',),('--confirmed-evidence',)]:
            r=self.cli('run-close','--repo',self.repo,'--epic','big','--path',one,
                       '--apply',*args,ok=False)
            self.assertNotEqual(r.returncode,0)
            self.assertTrue(one.exists())

    def test_run_close_does_not_touch_foreign_or_symlink_path(self):
        one=Path(self.cli('allocate','--repo',self.repo,'--epic','a','--kind','temp').stdout.strip())
        outside=self.dir/'user-owned';outside.mkdir();(outside/'source.txt').write_text('unique')
        for target in [outside,one]:
            r=self.cli('run-close','--repo',self.repo,'--epic','wrong','--path',target,
                       '--apply','--confirmed-idle','--confirmed-evidence',ok=False)
            self.assertEqual(3,r.returncode)
            self.assertIn('KEEP',r.stdout)
        self.assertTrue(one.exists())
        self.assertTrue((outside/'source.txt').exists())

    def test_unit_close_is_scoped_and_keeps_legacy_untagged(self):
        a=Path(self.cli('allocate','--repo',self.repo,'--epic','e','--unit','f1','--kind','probe').stdout.strip())
        b=Path(self.cli('allocate','--repo',self.repo,'--epic','e','--unit','f2','--kind','probe').stdout.strip())
        legacy=Path(self.cli('allocate','--repo',self.repo,'--epic','e','--kind','probe').stdout.strip())
        self.cli('unit-close','--repo',self.repo,'--epic','e','--unit','f1',
                 '--apply','--confirmed-idle','--confirmed-evidence')
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertTrue(legacy.exists())

    def test_unit_close_never_deletes_protected_evidence(self):
        a=Path(self.cli('allocate','--repo',self.repo,'--epic','e','--unit','f1','--kind','probe').stdout.strip())
        (a/'review.sqlite').write_text('not disposable')
        r=self.cli('unit-close','--repo',self.repo,'--epic','e','--unit','f1',
                   '--apply','--confirmed-idle','--confirmed-evidence',ok=False)
        self.assertEqual(3,r.returncode)
        self.assertIn('KEEP',r.stdout)
        self.assertTrue(a.exists())

    def test_unit_close_retire_only_integrated_worktree_of_unit(self):
        wt_a=self.setup_worktree('uwork')
        reg=self.workspace/'registry'/'worktrees'
        records=list(reg.glob('*.json'))
        self.assertEqual(len(records),1)
        data=json.loads(records[0].read_text())
        data['unit']='feature-a';records[0].write_text(json.dumps(data))
        self.cli('unit-close','--repo',self.repo,'--epic','uwork','--unit','feature-b',
                 '--apply','--confirmed-idle','--confirmed-evidence')
        self.assertTrue(wt_a.exists())
        self.cli('unit-close','--repo',self.repo,'--epic','uwork','--unit','feature-a',
                 '--apply','--confirmed-idle','--confirmed-evidence')
        self.assertFalse(wt_a.exists())

    def test_unit_close_preserves_dirty_worktree(self):
        wt=self.setup_worktree('dirtyunit')
        reg=list((self.workspace/'registry'/'worktrees').glob('*.json'))[0]
        data=json.loads(reg.read_text());data['unit']='f';reg.write_text(json.dumps(data))
        (wt/'private.patch').write_text('unique changes')
        r=self.cli('unit-close','--repo',self.repo,'--epic','dirtyunit','--unit','f',
                   '--apply','--confirmed-idle','--confirmed-evidence',ok=False)
        self.assertEqual(3,r.returncode)
        self.assertIn('KEEP worktree',r.stdout)
        self.assertTrue(wt.exists())

    def test_shared_scratch_close_only_clears_plugin_owned_temp_when_idle(self):
        import hashlib
        key = self.repo.name+'-'+hashlib.sha1(str(self.repo.resolve()).encode()).hexdigest()[:12]
        base = self.workspace/'temp'/'intelligence'/key
        for k in ('tmp','lanes','pycache'):
            (base/k).mkdir(parents=True)
            (base/k/'old.log').write_text('disposable')
        (base/'.opencode-workflow-scratch.json').write_text(json.dumps({
            'repo':str(self.repo.resolve()),'managedBy':'opencode-global-intelligence-v3'}))
        cache=self.workspace/'cache'/'intelligence'/'preserve';cache.mkdir(parents=True)
        (cache/'repo-intel.sqlite3').write_text('important reuse')
        r=self.cli('shared-scratch-close','--repo',self.repo,'--apply',ok=False)
        self.assertNotEqual(r.returncode,0)
        self.cli('shared-scratch-close','--repo',self.repo,
                 '--apply','--confirmed-project-idle','--confirmed-evidence')
        self.assertFalse((base/'tmp').exists())
        self.assertTrue((base/'.opencode-workflow-scratch.json').exists())
        self.assertTrue((cache/'repo-intel.sqlite3').exists())

    def test_unowned_shared_scratch_not_deleted(self):
        import hashlib
        key = self.repo.name+'-'+hashlib.sha1(str(self.repo.resolve()).encode()).hexdigest()[:12]
        tmp=self.workspace/'temp'/'intelligence'/key/'tmp';tmp.mkdir(parents=True)
        (tmp/'unique.txt').write_text('keep')
        self.cli('shared-scratch-close','--repo',self.repo,'--apply',
                 '--confirmed-project-idle','--confirmed-evidence')
        self.assertTrue((tmp/'unique.txt').exists())

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

    def test_unit_check_requires_real_disposition_and_does_not_delete(self):
        run=Path(self.cli('allocate','--repo',self.repo,'--epic','e','--unit','u').stdout.strip())
        args=('unit-check','--repo',self.repo,'--epic','e','--unit','u')
        r=self.cli(*args,ok=False)
        self.assertEqual(3,r.returncode)
        self.assertFalse(json.loads(r.stdout)['handoff_accounted'])
        self.assertTrue(run.exists())
        record=self.dir/'retention.json'
        record.write_text(json.dumps({'cleanup_retained':[{'path':str(run),'owner':'Builder','reason':'Reviewer still consumes trace','evidence':'review assignment 1','next_event':'review joined'}]}))
        r=self.cli(*args,'--retention-file',record)
        data=json.loads(r.stdout)
        self.assertTrue(data['handoff_accounted'])
        self.assertFalse(data['cleanup_complete'])
        self.assertTrue(run.exists())
        self.cli('unit-close','--repo',self.repo,'--epic','e','--unit','u','--retention-file',record,'--apply','--confirmed-idle','--confirmed-evidence')
        self.assertTrue(run.exists())
        self.cli('unit-close','--repo',self.repo,'--epic','e','--unit','u','--apply','--confirmed-idle','--confirmed-evidence')
        self.assertTrue(json.loads(self.cli(*args).stdout)['cleanup_complete'])

    def test_inventory_includes_legacy_and_foreign_units_without_deleting(self):
        run=Path(self.cli('allocate','--repo',self.repo,'--epic','e').stdout.strip())
        (run/'x').write_bytes(b'abc')
        legacy=self.config/'OpenCodeScratch'; legacy.mkdir();(legacy/'u').write_text('user')
        data=json.loads(self.cli('inventory','--sizes').stdout)
        bypath={r['path']:r for r in data['items']}
        self.assertEqual('managed_run',bypath[str(run)]['category'])
        self.assertEqual('legacy_inspect_only',bypath[str(legacy)]['category'])
        self.assertTrue(run.exists()); self.assertTrue(legacy.exists())

    def test_ignored_worktree_files_protect_user_data(self):
        wt=self.setup_worktree('ignored')
        # Ignore via the common Git exclude without altering tracked files.
        (self.repo/'.git'/'info'/'exclude').write_text('private-data.txt\n')
        (wt/'private-data.txt').write_text('unique')
        record=next((self.workspace/'registry'/'worktrees').glob('*.json'))
        data=json.loads(record.read_text());data['unit']='u';record.write_text(json.dumps(data))
        r=self.cli('unit-close','--repo',self.repo,'--epic','ignored','--unit','u','--apply','--confirmed-idle','--confirmed-evidence',ok=False)
        self.assertEqual(3,r.returncode)
        self.assertTrue((wt/'private-data.txt').exists())

    def test_retired_worktree_aux_is_removed_only_for_exact_owner(self):
        import hashlib
        wt=self.setup_worktree('aux')
        reg=next((self.workspace/'registry'/'worktrees').glob('*.json'))
        data=json.loads(reg.read_text());data['unit']='u';reg.write_text(json.dumps(data))
        def scratch(repo):
            key=repo.name+'-'+hashlib.sha1(str(repo).encode()).hexdigest()[:12]
            folder=self.workspace/'temp'/'intelligence'/key
            (folder/'tmp').mkdir(parents=True)
            (folder/'tmp'/'x').write_text('temporary')
            (folder/'.opencode-workflow-scratch.json').write_text(json.dumps({'repo':str(repo),'managedBy':'opencode-global-intelligence-v3'}))
            return folder
        owned=scratch(wt); shared=scratch(self.repo)
        self.cli('unit-close','--repo',self.repo,'--epic','aux','--unit','u','--apply','--confirmed-idle','--confirmed-evidence')
        self.assertFalse(wt.exists()); self.assertFalse(owned.exists()); self.assertFalse(reg.exists()); self.assertTrue(shared.exists())

    def test_retired_aux_protected_content_keeps_registry(self):
        import hashlib
        wt=self.setup_worktree('auxprotected')
        reg=next((self.workspace/'registry'/'worktrees').glob('*.json'))
        data=json.loads(reg.read_text());data['unit']='u';reg.write_text(json.dumps(data))
        key=wt.name+'-'+hashlib.sha1(str(wt).encode()).hexdigest()[:12]
        folder=self.workspace/'temp'/'intelligence'/key; folder.mkdir(parents=True)
        (folder/'.opencode-workflow-scratch.json').write_text(json.dumps({'repo':str(wt),'managedBy':'opencode-global-intelligence-v3'}))
        (folder/'unknown.db').write_text('unique')
        r=self.cli('unit-close','--repo',self.repo,'--epic','auxprotected','--unit','u','--apply','--confirmed-idle','--confirmed-evidence',ok=False)
        self.assertEqual(3,r.returncode); self.assertTrue(reg.exists()); self.assertTrue(folder.exists()); self.assertFalse(wt.exists())

if __name__=='__main__':unittest.main()
