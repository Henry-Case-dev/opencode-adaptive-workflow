"""Git-backed convergence edge cases; never approve or kill a worker."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_convergence import assess_task, audit_scope

D = 'a' * 64


def b(fid='F1'):
    return {'id': fid, 'classification': 'IN_SCOPE', 'contract_refs': ['TIME-03'],
            'evidence_ref': f'plans/features/time/evidence/{fid}.md'}


def review(commit, blockers, name):
    return {'candidate': commit, 'review_ref': f'plans/features/time/evidence/{name}.md',
            'reviewer_session': f'ses_{name}', 'verdict': 'Needs Fixes' if blockers else 'Approved',
            'contract_digest': D, 'blocking_findings': blockers}


def task(reviews=None, **more):
    c={'contract_digest':D, 'reviews':reviews or []}
    c.update(more)
    return {'id':'TIME-03','status':'REVIEW','convergence':c}


class GitConvergence(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='arbiter-proof-')
        self.root=Path(self.temp.name)
        self.git('init','-q')
        self.git('config','user.email','test@example.invalid')
        self.git('config','user.name','Test')
        (self.root/'app.py').write_text('value=0\n')
        self.base=self.commit('base')

    def tearDown(self): self.temp.cleanup()

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.STDOUT).decode().strip()

    def commit(self,label):
        self.git('add','.')
        self.git('commit','-qm',label)
        return self.git('rev-parse','HEAD')

    def change(self,text,label):
        (self.root/'app.py').write_text(text)
        return self.commit(label)

    def docs(self,label):
        p=self.root/'plans/features/time/evidence'
        p.mkdir(parents=True,exist_ok=True)
        (p/f'{label}.md').write_text(label)
        return self.commit(label)

    def state(self,x): return assess_task(x,self.root)

    def test_first_needs_fixes_no_arbiter(self):
        self.assertEqual(self.state(task([review(self.base,[b()],'a')]))['state'],'STABLE')

    def test_actual_second_failed_same_blockers_calls_once(self):
        nxt=self.change('value=1\n','repair-attempt')
        nxt2=self.change('value=2\n','second-repair-attempt')
        x=task([review(self.base,[b()],'a'),review(nxt,[b()],'b'),review(nxt2,[b()],'c')])
        r=self.state(x)
        self.assertEqual(r['state'],'CALL_ARBITER')
        self.assertEqual(r['triggers'][0]['kind'],'SAME_BLOCKER_AFTER_CORRECTIONS')
        key=r['triggers'][0]['trigger_key']
        x['convergence']['arbiter']={'state':'OPEN','trigger_key':key,'assignment_id':'asgn_ab1'}
        self.assertEqual(self.state(x)['state'],'ARBITER_CASE_OPEN')
        x['convergence']['arbiter']={'state':'RESOLVED','trigger_key':key,'decision_ref':'plans/features/time/evidence/arbiter.md'}
        self.assertEqual(self.state(x)['state'],'STABLE')

    def test_one_failed_correction_is_not_immediate_arbiter(self):
        nxt=self.change('value=1\n','fix-one')
        x=task([review(self.base,[b()],'a'),review(nxt,[b()],'b')])
        self.assertEqual(self.state(x)['state'],'STABLE')

    def test_docs_commit_does_not_create_repair(self):
        nxt=self.docs('summary')
        r=self.state(task([review(self.base,[b()],'a'),review(nxt,[b()],'b')]))
        self.assertEqual(r['state'],'STABLE')

    def test_same_commit_new_review_not_repair(self):
        r=self.state(task([review(self.base,[b()],'a'),review(self.base,[b()],'b')]))
        self.assertEqual(r['state'],'STABLE')

    def test_partial_improvement_not_arbiter(self):
        nxt=self.change('value=1\n','repair')
        r=self.state(task([review(self.base,[b('F1'),b('F2')],'a'),review(nxt,[b('F1')],'b')]))
        self.assertEqual(r['state'],'STABLE')

    def test_new_findings_not_auto_trigger(self):
        nxt=self.change('value=1\n','fix')
        r=self.state(task([review(self.base,[b('F1')],'a'),review(nxt,[b('F2')],'b')]))
        self.assertEqual(r['state'],'STABLE')

    def test_oscillation_detected(self):
        a=self.change('value=1\n','broken')
        b1=self.change('value=2\n','repaired')
        c=self.change('value=3\n','regressed')
        r=self.state(task([review(a,[b()],'a'),review(b1,[],'b'),review(c,[b()],'c')]))
        self.assertEqual(r['state'],'CALL_ARBITER')
        self.assertEqual(r['triggers'][0]['kind'],'OSCILLATING_CONTRACT')

    def test_review_approved_cannot_have_blockers(self):
        x=task([review(self.base,[],'a')]); x['convergence']['reviews'][0]['blocking_findings']=[b()]
        self.assertEqual(self.state(x)['state'],'INCOMPLETE')

    def test_missing_contract_digest_does_not_approve_or_call(self):
        x=task([review(self.base,[b()],'a')])
        x['convergence']['reviews'][0]['contract_digest']='b'*64
        self.assertEqual(self.state(x)['state'],'INCOMPLETE')

    def test_no_ancestry_is_unknown_not_stagnation(self):
        x=task([review(self.base,[b()],'a'),review('1234567',[b()],'b'),review('2345678',[b()],'c')])
        self.assertEqual(self.state(x)['state'],'NEEDS_EVIDENCE')

    def test_manual_escalation_works_even_when_git_history_unavailable(self):
        x=task([review(self.base,[b()],'a'),review('1234567',[b()],'b'),review('2345678',[b()],'c')],
               manual_escalation={'reason':'independently verified recurring contract disagreement',
                                  'evidence_ref':'plans/features/time/evidence/incident.md'})
        self.assertEqual(self.state(x)['state'],'CALL_ARBITER')

    def test_manual_trigger_when_no_prior_reviews(self):
        x=task([],manual_escalation={'reason':'Unresolved repeated candidate contract dispute',
                                    'evidence_ref':'plans/features/time/evidence/review.md'})
        self.assertEqual(self.state(x)['state'],'CALL_ARBITER')

    def test_manual_duplicate_does_not_respawn(self):
        x=task([],manual_escalation={'reason':'documented dispute','evidence_ref':'file.md'})
        t=self.state(x)['triggers'][0]['trigger_key']
        x['convergence']['arbiter']={'state':'OPEN','trigger_key':t,'assignment_id':'asgn_1'}
        self.assertEqual(self.state(x)['state'],'ARBITER_CASE_OPEN')

    def test_open_case_with_no_assignment_is_not_silent(self):
        x=task([],manual_escalation={'reason':'documented dispute','evidence_ref':'file.md'},
                arbiter={'state':'OPEN','trigger_key':'some-key'})
        self.assertEqual(self.state(x)['state'],'NEEDS_EVIDENCE')

    def test_invalid_reviewer_entry_never_trigger(self):
        nxt=self.change('value=1\n','code')
        x=task([review(self.base,[b()],'a'),review(nxt,[b()],'b')])
        del x['convergence']['reviews'][0]['reviewer_session']
        self.assertEqual(self.state(x)['state'],'INCOMPLETE')

    def test_legacy_rows_nonblocking(self):
        x={'tasks':[{'id':'a','status':'PENDING'},task([review(self.base,[b()],'a')])]}
        snap=audit_scope(x,self.root)
        self.assertFalse(snap['needs_arbiter'])
        self.assertEqual(snap['legacy_unrecorded'],1)

    def test_closed_task_never_reopens(self):
        x=task([review(self.base,[b()],'a')])
        x['status']='VERIFIED'
        self.assertEqual(self.state(x)['state'],'CLOSED')


if __name__ == '__main__': unittest.main()
