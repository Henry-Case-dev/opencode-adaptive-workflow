import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import workflow_checkpoint as wc

SCHEMA = 'openworkflow-progress-scope-v1'

def task(id, status, position=None, blockers=None, execution_state=None):
    result = {'id': id, 'status': status}
    if position is not None:
        result.update(queue_position=position, priority='P0', blocked_by=blockers if blockers is not None else [])
    if execution_state:
        result['execution_state'] = execution_state
    return result

class CheckpointRepair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        (self.repo / 'plans').mkdir()
        self.path = self.repo / 'plans' / 'workflow_state.md'
        self.scope = self.repo / 'plans' / 'progress_scope.json'
        self.scope.write_text(json.dumps({'schema': SCHEMA, 'tasks': [task('P0', 'VERIFIED'), task('C2', 'PENDING', 46, ['P0'])]}))
        self.path.write_text('Task: P0\nStatus: done\nHuman gate: none\nNext execution: awaiting_children\nNext execution: none\n## Historical\nStatus: old\n')
    def tearDown(self):
        self.temp.cleanup()
    def runcli(self, *args):
        return subprocess.run([sys.executable, str(Path(wc.__file__)), '--repo', str(self.repo), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def test_resolves_completed_dependency_only(self):
        self.assertEqual(wc.ready_tasks(json.loads(self.scope.read_text())), ['C2'])
        self.scope.write_text(json.dumps({'schema':SCHEMA,'tasks':[task('P0','WAITING_EXTERNAL'),task('C2','PENDING',46,['P0'])]}))
        self.assertEqual(wc.ready_tasks(json.loads(self.scope.read_text())),[])
    def test_cas_repair_preserves_history(self):
        read = self.runcli()
        self.assertEqual(read.returncode, 0, read.stderr)
        source = json.loads(read.stdout)
        self.assertTrue(source['safe'])
        self.assertFalse(source['applied'])
        before = self.path.read_text()
        result = self.runcli('--apply', '--expected-sha256', source['sha256'])
        self.assertEqual(result.returncode, 0, result.stdout)
        text = self.path.read_text()
        head, body = wc.split_header(text)
        self.assertEqual(wc.fields(head)['status'], ['in_progress'])
        self.assertEqual(wc.fields(head)['next execution'], ['runnable'])
        self.assertEqual(wc.fields(head)['human gate'], ['none'])
        self.assertEqual(body, '## Historical\nStatus: old\n')
        self.assertIn('awaiting_children', before)
        self.assertFalse((self.repo/'plans'/'.workflow_state.md.lock').exists())
    def test_stale_cas_must_not_write(self):
        self.assertNotEqual(self.runcli('--apply','--expected-sha256','0'*64).returncode,0)
        self.assertIn('awaiting_children',self.path.read_text())
    def test_duplicate_human_gate_fails_closed(self):
        self.path.write_text('Status: done\nHuman gate: none\nHuman gate: none\nNext execution: none\n')
        self.assertFalse(json.loads(self.runcli().stdout)['safe'])
        self.assertNotEqual(self.runcli('--apply', '--expected-sha256', wc.sha(self.path.read_bytes())).returncode,0)
    def test_genuine_human_gate_fails_closed(self):
        self.path.write_text('Status: done\nHuman gate: BLOCKED_REAL_HUMAN_GATE\nNext execution: none\n')
        self.assertFalse(json.loads(self.runcli().stdout)['safe'])
    def test_no_admitted_task_does_not_create_one(self):
        self.scope.write_text(json.dumps({'schema':SCHEMA,'tasks':[task('P0','VERIFIED'),task('C2','PENDING',None,[])]}))
        self.assertFalse(json.loads(self.runcli().stdout)['safe'])
    def test_duplicate_active_queue_position_is_not_certified(self):
        self.scope.write_text(json.dumps({'schema':SCHEMA,'tasks':[task('C2','PENDING',1,[]),task('C3','PENDING',1,[])]}))
        self.assertFalse(json.loads(self.runcli().stdout)['safe'])
    def test_cancelled_does_not_satisfy_predecessor(self):
        self.scope.write_text(json.dumps({'schema':SCHEMA,'tasks':[task('P0','CANCELLED'),task('C2','PENDING',2,['P0'])]}))
        self.assertFalse(json.loads(self.runcli().stdout)['safe'])
    def test_running_assignment_not_certified(self):
        self.scope.write_text(json.dumps({'schema':SCHEMA,'tasks':[task('C2','PENDING',2,[], 'running')]}))
        self.assertFalse(json.loads(self.runcli().stdout)['safe'])
    def test_repeat_repair_is_idempotent(self):
        sha = json.loads(self.runcli().stdout)['sha256']
        self.assertEqual(self.runcli('--apply','--expected-sha256',sha).returncode,0)
        sha2 = wc.sha(self.path.read_bytes())
        result=json.loads(self.runcli('--apply','--expected-sha256',sha2).stdout)
        self.assertFalse(result['changed'])

if __name__=='__main__':unittest.main()

class MalformedHeaderProof(unittest.TestCase):
    def test_missing_and_corrupt_fields_cannot_be_repaired(self):
        scope={'schema':SCHEMA,'tasks':[task('c2','PENDING',1,[])]}
        for text in ['Human gate: none\nNext execution: none',
                     'Status: done\nHuman gate: none',
                     'Status: forged\nHuman gate: none\nNext execution: none']:
            self.assertFalse(wc.state(scope,text)['safe'])

class CooperativeLock(unittest.TestCase):
    def test_existing_lock_is_never_removed_by_another_writer(self):
        with tempfile.TemporaryDirectory() as root:
            repo=Path(root);plans=repo/'plans';plans.mkdir()
            chk=plans/'workflow_state.md';chk.write_text('Status: done\nHuman gate: none\nNext execution: none\n')
            (plans/'progress_scope.json').write_text(json.dumps({'schema':SCHEMA,'tasks':[task('c2','PENDING',1,[])]}))
            lock=plans/'.workflow_state.md.lock';lock.write_text('existing-owner')
            result=subprocess.run([sys.executable,str(Path(wc.__file__)), '--repo',str(repo),
                                   '--apply','--expected-sha256',wc.sha(chk.read_bytes())],
                                  capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(lock.read_text(),'existing-owner')
