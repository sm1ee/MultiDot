import contextlib
import copy
import json
import pathlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from multidot.controller import Controller
from multidot.models import ConflictError, ValidationError
from multidot.dot2api_adapter import TransportUncertain, UpstreamRejected

class MockAdapter:
    producer='hub-producer'
    target='mock://independent-review'
    token=''
    def __init__(self): self.tasks={}; self.bodies=[]
    def submit(self,body):
        self.bodies.append(copy.deepcopy(body))
        key=body['idempotency_key']
        if key not in self.tasks:
            self.tasks[key]=dict(copy.deepcopy(body),id='task-'+str(len(self.tasks)+1),status='queued',attempts=0,revision=1,result=None,error=None)
        return copy.deepcopy(self.tasks[key])
    def get(self,task_id):
        return copy.deepcopy(next(t for t in self.tasks.values() if t['id']==task_id))
    def cancel(self,task_id,reason):
        t=next(t for t in self.tasks.values() if t['id']==task_id)
        t.update(status='cancelled',revision=t['revision']+1)
        return copy.deepcopy(t)

def fixture():
    snap={'schema_version':'multidot.snapshot.v1','project_id':'review','snapshot_id':'v1','artifacts':[{'name':'input.md','media_type':'text/markdown','content':'Two provided design options.'}]}
    job={'schema_version':'multidot.job.v1','project_id':'review','request_id':'req-1','title':'Review','goal':'Compare provided options','input_snapshot_id':'v1','policy_id':'provided-materials-only','steps':[{'step_id':'b','worker':'dot-b','instructions':'Compare the options','acceptance_criteria':['Evidence cited'],'depends_on':[]}]}
    return snap,job

def result(job,step='b',name='report.md'):
    return {'schema_version':'multidot.result.v1','job_id':job,'step_id':step,'input_snapshot_id':'v1','outcome':'completed','summary':'Compared','findings':[{'claim':'One distinction','evidence_ref':'input.md'}],'artifacts':[{'name':name,'media_type':'text/markdown','content':'Comparison of provided text.'}],'checks':[{'name':'Inputs compared','status':'passed','evidence_ref':'input.md'}],'open_questions':[],'external_changes':[]}

class Independent(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=pathlib.Path(self.temp.name)/'hub.sqlite'; self.adapter=MockAdapter(); self.c=Controller(self.path,self.adapter,['review']); self.c.bootstrap('review','Test'); self.snap,self.spec=fixture(); self.c.add_snapshot(self.snap)
    def tearDown(self): self.c.close(); self.temp.cleanup()
    def submit_dispatch(self):
        job=self.c.submit_job(self.spec)['job_id']; attempt=self.c.schedule()[0]; self.c.dispatch(); task=next(iter(self.adapter.tasks.values())); return job,attempt,copy.deepcopy(task)
    def test_request_and_snapshot_immutability(self):
        a=self.c.submit_job(self.spec); b=self.c.submit_job(copy.deepcopy(self.spec)); self.assertEqual(a['job_id'],b['job_id']); self.assertTrue(b['duplicate'])
        changed=copy.deepcopy(self.spec); changed['goal']='Other goal'
        with self.assertRaises(ConflictError): self.c.submit_job(changed)
        changed=copy.deepcopy(self.snap); changed['artifacts'][0]['content']='Changed'
        with self.assertRaises(ConflictError): self.c.add_snapshot(changed)
    def test_atomic_concurrent_reservation_cap(self):
        for i in range(12):
            spec=copy.deepcopy(self.spec); spec['request_id']='req-'+str(i); self.c.submit_job(spec)
        barrier=threading.Barrier(6)
        def schedule(_):
            c=Controller(self.path,MockAdapter(),['review'])
            try: barrier.wait(); return c.schedule()
            finally: c.close()
        with ThreadPoolExecutor(max_workers=6) as pool: outcomes=list(pool.map(schedule,range(6)))
        self.assertEqual(sum(len(x) for x in outcomes),1)
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1)
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM dispatch_outbox').fetchone()[0],1)
    def test_stale_observation_and_required_artifact(self):
        job,attempt,task=self.submit_dispatch(); running=copy.deepcopy(task); running.update(status='running',attempts=1,revision=3); self.c.observe(attempt,running); self.c.observe(attempt,task)
        self.assertEqual(self.c.db.execute('SELECT revision FROM attempts').fetchone()[0],3)
        completed=copy.deepcopy(running); completed.update(status='completed',revision=4,result=result(job,name='missing.md')); self.c.observe(attempt,completed)
        self.assertEqual(self.c.get_job(job)['state'],'NEEDS_REVIEW')
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],0)
    def test_synthesis_once_for_accepted_versions(self):
        self.spec['synthesis']={'worker':'dot-a','trigger':'all_required_steps_accepted','instructions':'Compare actual results'}
        job,attempt,task=self.submit_dispatch(); task.update(status='completed',attempts=1,revision=3,result=result(job)); self.c.observe(attempt,task); self.c.observe(attempt,task); self.c.schedule(); self.c.schedule()
        self.assertEqual(self.c.db.execute("SELECT COUNT(*) FROM steps WHERE kind='synthesis'").fetchone()[0],1)
        self.assertEqual(self.c.db.execute("SELECT COUNT(*) FROM attempts WHERE worker='dot-a'").fetchone()[0],1)
    def test_cancel_before_send_blocks_all_future_dispatch(self):
        self.spec['synthesis']={'worker':'dot-a','trigger':'all_required_steps_accepted','instructions':'Compare actual results'}
        job=self.c.submit_job(self.spec)['job_id']; self.c.schedule(); state=self.c.request_cancel(job,'Cancel requested'); self.c.tick()
        self.assertEqual(state['state'],'CANCELLED'); self.assertFalse(state['worker_stop_confirmed']); self.assertEqual(self.adapter.bodies,[])
    def test_uncertain_submission_retry_preserves_exact_body_and_slot(self):
        job=self.c.submit_job(self.spec)['job_id']; attempt=self.c.schedule()[0]
        original=self.adapter.submit
        def submit_and_drop(body):
            original(body); raise TransportUncertain()
        self.adapter.submit=submit_and_drop; self.c.dispatch()
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1)
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],1)
        self.c.close(); self.c=Controller(self.path,self.adapter,['review']); self.adapter.submit=original
        self.c.db.execute('UPDATE dispatch_outbox SET next_try=0'); self.c.dispatch()
        self.assertEqual(len(self.adapter.tasks),1)
        self.assertEqual(self.adapter.bodies[0],self.adapter.bodies[1])
        self.assertEqual(self.c.db.execute('SELECT state FROM dispatch_outbox').fetchone()[0],'CONFIRMED')
    def test_limit_error_does_not_fail_over_accounts(self):
        job=self.c.submit_job(self.spec)['job_id']; self.c.schedule()
        def refused(body): raise UpstreamRejected(429)
        self.adapter.submit=refused; self.c.dispatch(); self.c.tick()
        self.assertEqual(self.c.get_job(job)['state'],'NEEDS_REVIEW')
        attempts=self.c.db.execute('SELECT worker,attempt_no FROM attempts').fetchall()
        self.assertEqual([tuple(a) for a in attempts],[('dot-b',1)])
        self.assertEqual(self.c.db.execute("SELECT paused FROM workers WHERE id='dot-b'").fetchone()[0],1)
    def test_delayed_read_error_cannot_downgrade_concurrent_terminal_result(self):
        job,attempt,task=self.submit_dispatch(); secondary=Controller(self.path,self.adapter,['review'])
        completed=copy.deepcopy(task); completed.update(status='completed',attempts=1,revision=3,result=result(job))
        def delayed_failure(_):
            secondary.observe(attempt,completed)
            raise TransportUncertain('Delayed failure from older read')
        self.adapter.get=delayed_failure
        try: self.c.reconcile()
        finally: secondary.close()
        self.assertEqual(self.c.get_job(job)['state'],'COMPLETED')
        self.assertEqual(self.c.db.execute('SELECT state FROM dispatch_outbox').fetchone()[0],'CONFIRMED')
    def test_cancellation_after_completion_preserves_successful_terminal_state(self):
        job,attempt,task=self.submit_dispatch(); task.update(status='completed',attempts=1,revision=3,result=result(job)); self.c.observe(attempt,task)
        self.c.request_cancel(job,'Cancellation arrived too late')
        self.assertEqual(self.c.get_job(job)['state'],'COMPLETED')
        self.assertEqual(self.c.get_job(job)['delivery'],'notification_pending')
    def test_delayed_final_submit_error_cannot_downgrade_confirmed_completion(self):
        job=self.c.submit_job(self.spec)['job_id']; attempt=self.c.schedule()[0]
        self.c.db.execute('UPDATE dispatch_outbox SET sends=4')
        secondary=Controller(self.path,self.adapter,['review']); original=self.adapter.submit
        def submit_then_delayed_failure(body):
            completed=original(body); completed.update(status='completed',attempts=1,revision=3,result=result(job))
            self.adapter.tasks[body['idempotency_key']]=completed
            secondary.observe(attempt,completed)
            raise TransportUncertain('Delayed older submission response')
        self.adapter.submit=submit_then_delayed_failure
        try: self.c.dispatch()
        finally: secondary.close()
        self.assertEqual(self.c.get_job(job)['state'],'COMPLETED')
        self.assertEqual(self.c.db.execute('SELECT state FROM dispatch_outbox').fetchone()[0],'CONFIRMED')
    def test_crash_during_fifth_send_recovers_to_review_without_sixth_send(self):
        job=self.c.submit_job(self.spec)['job_id']; self.c.schedule()
        self.c.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=5")
        self.c.close(); self.c=Controller(self.path,self.adapter,['review'])
        self.c.recover(apply=True); self.c.dispatch()
        self.assertEqual(self.c.get_job(job)['state'],'NEEDS_REVIEW')
        self.assertEqual(self.c.db.execute('SELECT state FROM dispatch_outbox').fetchone()[0],'REVIEW')
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1)
        self.assertEqual(self.adapter.bodies,[])
    def test_unexpected_upstream_cancellation_does_not_look_unstarted(self):
        job,attempt,task=self.submit_dispatch(); task.update(status='cancelled',revision=2); self.c.observe(attempt,task)
        self.assertIn(self.c.get_job(job)['state'],('CANCELLED','NEEDS_REVIEW'))
    def test_terminal_state_is_fenced_under_concurrent_observation(self):
        job,attempt,queued=self.submit_dispatch(); secondary=Controller(self.path,self.adapter,['review']); original=self.c.store.transaction
        completed=copy.deepcopy(queued); completed.update(status='cancelled',attempts=1,revision=2)
        running=copy.deepcopy(queued); running.update(status='running',attempts=1,revision=3)
        invoked=False
        @contextlib.contextmanager
        def interleaved():
            nonlocal invoked
            if not invoked:
                invoked=True; secondary.observe(attempt,completed)
            with original() as db: yield db
        self.c.store.transaction=interleaved
        try: self.c.observe(attempt,running)
        finally: self.c.store.transaction=original; secondary.close()
        actual=self.c.db.execute('SELECT upstream_status FROM attempts').fetchone()[0]
        self.assertEqual(actual,'cancelled','Terminal state regressed while the observation waited for its write transaction')

if __name__=='__main__': unittest.main(verbosity=2)
