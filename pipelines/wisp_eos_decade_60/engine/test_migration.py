import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import migrate as m, quiesce as q
class Tests(unittest.TestCase):
 def test_dates_and_pilot_months(self):
  ts=m.month_tasks({'start':'2014-01-12','end_exclusive':'2024-01-01','month_reservation_bytes':8*2**30},'T000830')
  self.assertEqual(len(ts),120);self.assertEqual(sum(len(t['dates']) for t in ts.values()),3641)
  self.assertEqual(ts['T000830_2014-01']['dates'][0],'2014-01-12T00:00:00')
  self.assertIn('2020-02-29T00:00:00',ts['T000830_2020-02']['dates'])
  self.assertEqual(sum(t['priority']<0 for t in ts.values()),12)
 def test_capacity_independent_statuses(self):
  with tempfile.TemporaryDirectory() as td:
   c={'scratch':td,'month_reservation_bytes':8*2**30,'shared_cache_reserve_bytes':16*2**30,'scratch_budget_bytes':1024*2**30,'scratch_min_free_bytes':0}
   ledger={'active_cube':'A','tasks':{'A_2014-01':{'region':'A','kind':'month','prediction_status':'done','input_status':'queued'},'B_2014-01':{'region':'B','kind':'month'}}}
   r=m.capacity(c,ledger,{str(Path(td)/'inputs/B_2014-01'):100},200)
   self.assertEqual(ledger['tasks']['A_2014-01']['reserved'],8*2**30)
   self.assertEqual(ledger['tasks']['B_2014-01']['reserved'],100);self.assertEqual(ledger['quarantine_bytes'],200)
   self.assertTrue(r['passed']);self.assertEqual(r['parked_allocated_bytes'],100)
 def test_overbudget_counts_unregistered_inputs(self):
  with tempfile.TemporaryDirectory() as td:
   c={'scratch':td,'month_reservation_bytes':8*2**30,'shared_cache_reserve_bytes':16*2**30,'scratch_budget_bytes':1024*2**30,'scratch_min_free_bytes':0,'start':'2014-01-12','end_exclusive':'2024-01-01'}
   ledger={'active_cube':'A','tasks':m.month_tasks(c,'A')}
   r=m.capacity(c,ledger,{str(Path(td)/'inputs/orphan'):49*2**30},0)
   self.assertFalse(r['passed']);self.assertEqual(r['excess_bytes'],2**30)
 def test_copy_preserves_source_and_rejects_conflict(self):
  with tempfile.TemporaryDirectory() as td:
   a,b=Path(td)/'a',Path(td)/'b';a.write_text('original');m.copy_file(a,b);m.copy_file(a,b)
   self.assertEqual(a.read_text(),b.read_text());b.write_text('changed')
   with self.assertRaises(ValueError):m.copy_file(a,b)
 def test_complete_never_resets_live_queue(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);q.atomic(root/'code/migration/complete.json',{'old_root':'/old','new_root':str(root),'epoch':'e','passed':True})
   q.atomic(root/'state/queue.json',{'phase':'PREDICT','tasks':{'x':{'job':'123'}}})
   before=(root/'state/queue.json').read_bytes();m.migrate('/old','/ops',root)
   self.assertEqual(before,(root/'state/queue.json').read_bytes())
 def test_cancellation_rejects_wrong_script(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);q.atomic(root/'state/submissions/123.json',{'job':'123','kind':'cpu','controller_release':'/ops','args':['/ops/job64.sh',str(root/'config/deployment.json'),'cpu']})
   with patch.object(q,'run',side_effect=['123|wispb1-cpu|RUNNING\n','JobId=123 UserId='+q.getpass.getuser()+'(1) Command=/other/job64.sh']):
    with self.assertRaises(ValueError):q.owned_jobs(root,Path('/ops'))
 def test_cancellation_accepts_exact_job_only(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);q.atomic(root/'state/submissions/123.json',{'job':'123','kind':'cpu','controller_release':'/ops','args':['/ops/job64.sh',str(root/'config/deployment.json'),'cpu']})
   with patch.object(q,'run',side_effect=['123|wispb1-cpu|RUNNING\n999|unrelated|RUNNING\n','JobId=123 UserId='+q.getpass.getuser()+'(1) Command=/ops/job64.sh']):
    self.assertEqual([j['id'] for j in q.owned_jobs(root,Path('/ops'))],['123'])
 def test_scheduler_stops_before_workers(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);audit=root/'audit';q.atomic(root/'state/queue.json',{'tasks':{}})
   jobs=[{'id':'1','kind':'control','state':'RUNNING'},{'id':'2','kind':'cpu','state':'RUNNING'}]
   with patch.object(q,'owned_jobs',return_value=jobs),patch.object(q,'checkpoints',return_value={}),patch.object(q,'run',return_value='') as mock:
    self.assertFalse(q.step(root,Path('/ops'),audit,quick=True));self.assertEqual(mock.call_args[0][0],['scancel','1'])
if __name__=='__main__':unittest.main(verbosity=2)
