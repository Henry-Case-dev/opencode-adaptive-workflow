"""Finite delivery, mixed legacy state, scoped recovery and index behavior.
Run in disposable local Git repositories; no production/network calls.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import workflow_progress as p
import workflow_checkpoint as checkpoint
import requirements_gate as gate

ROOT=Path(__file__).resolve().parents[1]
SCHEMA=p.SCOPE_SCHEMA
SHA='a'*40
REF='b'*40+':plans/evidence.md'

def row(tid, status, pos=1, priority='P0', blockers=None, **extra):
    return dict(id=tid, status=status, queue_position=pos, priority=priority, blocked_by=[] if blockers is None else blockers, **extra)

def delivered(**extra):
    return dict(kind='deployment', candidate=SHA, deployed_commit=SHA, review_ref=REF, delivery_ref=REF, result='Доставлено', deferred_req_ids=[], **extra)

class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'plans').mkdir()
        self.scope=self.root/'plans'/'progress_scope.json'
    def snapshot(self, rows, **extra):
        self.scope.write_text(json.dumps(dict(schema=SCHEMA,tasks=rows,**extra)),encoding='utf-8')
        return p.snapshot([], 'all', str(self.scope))
    def test_historical_closed_never_requires_new_receipt(self):
        rows=[row('old','VERIFIED', maps=['plans/map.json'], delivery_gate_ref='plans/missing.txt'),row('next','READY',2,blockers=['old'])]
        mp=self.root/'plans'/'map.json';mp.write_text(json.dumps(dict(schema=p.SCHEMA,items=[dict(req_id='R1',disposition='required',slice='A',status='VERIFIED')],additional_requirements=[])))
        a=self.snapshot(rows)
        mp.write_text(mp.read_text()+'\n')
        b=p.snapshot([], 'all', str(self.scope))
        self.assertEqual(a['tasks'],b['tasks']);self.assertEqual(b['tasks'],{'done':1,'total':2})
        self.assertEqual(b['engineering_delivery']['legacy_closed_task_ids'],['old'])
        self.assertEqual(b['engineering_delivery']['reconcile_task_ids'],[])
        self.assertEqual(b['engineering_queue']['ready_task_ids'],['next'])
        mp.unlink();c=p.snapshot([], 'all', str(self.scope))
        self.assertEqual(c['tasks'],b['tasks']);self.assertFalse(c['requirements_measurable'])
    def test_corrupt_optional_map_never_erases_task_progress(self):
        (self.root/'plans'/'map.json').write_text('[]')
        s=self.snapshot([row('old','VERIFIED',maps=['plans/map.json']),row('next','READY',2)])
        self.assertEqual(s['tasks'],{'done':1,'total':2})
        self.assertFalse(s['requirements_measurable']);self.assertTrue(s['map_diagnostics'])
        self.assertEqual(s['engineering_queue']['ready_task_ids'],['next'])
    def test_new_done_record_required_and_no_mutable_hash_dependency(self):
        good=delivered();good['deferred_req_ids']=['external-1']
        bad=delivered();bad['deployed_commit']='c'*40
        s=self.snapshot([row('good','DONE',delivery=good),row('bad','DONE',2,delivery=bad),row('dep','READY',3,blockers=['good']),row('other','READY',4,blockers=['bad'])])
        self.assertEqual(s['tasks'],{'done':1,'total':4})
        self.assertEqual(s['engineering_delivery']['external_evidence_task_ids'],['good'])
        self.assertEqual(s['engineering_delivery']['reconcile_task_ids'],['bad'])
        self.assertEqual(s['engineering_queue']['ready_task_ids'],['dep'])
    def test_old_parked_is_not_silently_new_done(self):
        s=self.snapshot([row('parked','WAITING_EXTERNAL',execution_state='delivered_awaiting_evidence',delivery_gate_ref='old.txt'),row('other','READY',2)])
        self.assertEqual(s['tasks']['done'],0)
        self.assertEqual(s['engineering_queue']['task_ids'],['parked','other'])
        self.assertEqual(s['engineering_queue']['ready_task_ids'],['other'])
    def test_bad_rows_are_local_and_priority_before_position(self):
        rows=[row('P3','READY',1,'P3'),row('P0','READY',9,'P0'),row('equal','READY',9,'P0'),row('duplicate','READY'),row('duplicate','VERIFIED'),row('dep','READY',4,blockers=['duplicate']),row('unknown','GIBBERISH'),row('missing','READY',5,blockers=['missing-id']),None]
        s=self.snapshot(rows)
        self.assertEqual(s['engineering_queue']['ready_task_ids'],['P0','equal','P3'])
        self.assertFalse(s['queue_registration_complete']);self.assertEqual(s['tasks']['done'],0)
    def test_pending_amendment_preserves_history_but_holds_affected_dependencies(self):
        rows=[row('old','VERIFIED'),row('dep','READY',2,blockers=['old']),row('independent','READY',3)]
        s=self.snapshot(rows,owner_inputs=[dict(id='new',reconciled=False,target_task_ids=['old'])])
        self.assertEqual(s['tasks'],{'done':1,'total':3});self.assertEqual(s['intake_pending'],['new'])
        self.assertEqual(s['engineering_queue']['ready_task_ids'],['independent'])
    def test_scoped_gate_preserves_header_and_other_task(self):
        rows=[row('blocked','BLOCKED_REAL_HUMAN_GATE'),row('ready','READY',2),row('dep','READY',3,blockers=['blocked'])]
        scope=dict(schema=SCHEMA,tasks=rows)
        text='Status: blocked\nHuman gate: task (blocked)\nNext execution: none\n## History\nold\n'
        self.assertEqual(checkpoint.state(scope,text)['ready'],['ready']);self.assertTrue(checkpoint.state(scope,text)['safe'])
        self.assertIn('Human gate: task (blocked)',checkpoint.repair_header(text))
        self.assertFalse(checkpoint.state(scope,text.replace('(blocked)','(unknown)'))['safe'])
        self.assertFalse(checkpoint.state(scope,text.replace('task (blocked)','approval'))['safe'])
    def test_no_deep_audit_on_normal_snapshot(self):
        with patch.object(p,'audit_delivery_receipts',side_effect=AssertionError('deep delivery audit')),patch.object(p,'audit_source_inventory',side_effect=AssertionError('deep source audit')):
            self.assertEqual(self.snapshot([row('old','VERIFIED')])['tasks']['done'],1)
    def test_cross_runtime_ready_parity(self):
        rows=[row('legacy','VERIFIED'),row('closed','DONE',delivery=delivered()),row('z','READY',1,'P3'),row('a','READY',5,'P0',blockers=['closed']),row('b','READY',5,'P0',blockers=['legacy']),row('bad','DONE'),row('c','READY',6,blockers=['bad']),row('duplicate','READY'),row('duplicate','VERIFIED'),row('d','READY',3,blockers=['duplicate'])]
        sc=dict(schema=SCHEMA,tasks=rows)
        js=ROOT/'plugins'/'continuation-guard'/'core.mjs'
        command=['node','--input-type=module','-e',f'import {{certifiedReadyQueueIds}} from {json.dumps(js.as_uri())}; let s="";for await(const c of process.stdin)s+=c; console.log(JSON.stringify(certifiedReadyQueueIds(JSON.parse(s))))']
        try: out=subprocess.run(command,input=json.dumps(sc),text=True,capture_output=True,check=True)
        except FileNotFoundError: self.skipTest('node unavailable')
        self.assertEqual(json.loads(out.stdout),p.ready_task_ids(sc))

class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.repo=Path(self.temp.name)
        self.git('init','-q');self.git('config','user.name','Harness test');self.git('config','user.email','test@example.invalid')
        (self.repo/'code.py').write_text('def original(): return 1\n');(self.repo/'review.md').write_text('Approved candidate (synthetic fixture)\n');(self.repo/'delivery.md').write_text('Delivered (synthetic fixture)\n')
        self.git('add','.');self.git('commit','-qm','initial');self.head=self.git('rev-parse','HEAD')
    def git(self,*args):
        return subprocess.run(['git',*args],cwd=self.repo,capture_output=True,text=True,check=True).stdout.strip()
    def delivery_cmd(self,*extra,kind='deployment',deployed=True,review='HEAD:review.md'):
        args=[sys.executable,str(ROOT/'tools'/'requirements_gate.py'),'delivery-record','--repo',str(self.repo),'--kind',kind,'--candidate',self.head,'--review-ref',review,'--delivery-ref','HEAD:delivery.md','--result','Fixture']
        if deployed: args+=['--deployed-commit',self.head]
        return subprocess.run(args+list(extra),capture_output=True,text=True)
    def test_delivery_ref_immutable_and_mismatch_rejected(self):
        r=self.delivery_cmd();self.assertEqual(r.returncode,0,r.stderr);d=json.loads(r.stdout);self.assertTrue(p.delivery_recorded(dict(delivery=d)))
        self.assertEqual(d['review_ref'],self.head+':review.md')
        (self.repo/'review.md').write_text('changed working copy')
        self.assertEqual(self.delivery_cmd().returncode,0)
        self.assertNotEqual(self.delivery_cmd(review='HEAD:missing.md').returncode,0)
        self.assertNotEqual(self.delivery_cmd(review='HEAD:../review.md').returncode,0)
        self.git('add','review.md');self.git('commit','-qm','another commit')
        self.assertNotEqual(self.delivery_cmd('--deployed-commit','HEAD').returncode,0)
    def test_non_deployment_record(self):
        r=self.delivery_cmd(kind='non_deployment',deployed=False);self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue(p.delivery_recorded(dict(delivery=json.loads(r.stdout))))
        self.assertNotEqual(self.delivery_cmd(kind='non_deployment').returncode,0)
    def test_index_revert_untracked_delete_and_no_binary_hash(self):
        path=ROOT/'plugins'/'workflow-intelligence'/'workflow_intel.py'
        spec=importlib.util.spec_from_file_location('workflow_intel_v4',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as cache,patch.dict(os.environ,{'OPENCODE_WORKFLOW_CACHE_ROOT':cache}):
            db=m.connect(self.repo);self.addCleanup(db.close);m.refresh(self.repo,db)
            code=self.repo/'code.py';code.write_text('def changed(): return 2\n')
            (self.repo/'extra.py').write_text('def extra(): return 3\n');(self.repo/'archive.zip').write_bytes(b'not indexed')
            original=m._hash_file
            def guarded(path):
                self.assertNotEqual(path.name,'archive.zip');return original(path)
            with patch.object(m,'_hash_file',side_effect=guarded):m.refresh(self.repo,db)
            self.assertTrue(m.locate(db,'changed',10));self.assertTrue(m.locate(db,'extra',10))
            self.git('checkout','--','code.py');(self.repo/'extra.py').unlink();m.refresh(self.repo,db)
            self.assertTrue(m.locate(db,'original',10));self.assertFalse(m.locate(db,'changed',10));self.assertFalse(m.locate(db,'extra',10))
            self.assertEqual(m.refresh(self.repo,db)['changed'],0)

if __name__=='__main__':unittest.main()
