"""Top up missing month producers after a recorded repair, within global 120 jobs.

A failed child may leave its job's other child working for hours. Such parent
jobs still count toward the usual lane cap. This one-shot recovery submits only
ceil(unowned queued months / 2) extra jobs and preserves the global 120 CPU cap.
"""
import argparse
import math
import os
from pathlib import Path
import subprocess
from campaign import engine, read, atomic, utc


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path);args=p.parse_args()
    root=args.root.resolve();shared=root/'campaigns/dual120_v1';b=root/'lanes/lane_b'
    if not (shared/'index_hardlinks.json').exists():raise ValueError('Repair audit required')
    rt,ctl,_,c=engine(b)
    def jobs(config, all_lanes=False):
        rows=subprocess.check_output(['squeue','-h','-r','-u','ybai','-o','%i|%j|%T'],text=True)
        result=[]
        for row in rows.splitlines():
            jid,name,state=row.split('|')
            if not name.startswith(('wisp1c-','wisp2c-')):continue
            locations=[r for r in [root,b] if (r/'state/submissions'/(jid+'.json')).exists()]
            if len(locations)!=1:raise ValueError('Unregistered job')
            r=locations[0];record=read(r/'state/submissions'/(jid+'.json'))
            if record['job']!=jid or record['name']!=name:raise ValueError('Registry mismatch')
            if all_lanes or r==b:result.append(dict(id=jid,name=name,state=state,kind=record['kind']))
        return result
    ctl.jobs=jobs;ctl.PREFIX='wisp2c-';ctl.CPU_CAP=120
    submitted=[]
    with rt.locked(shared/'submit.lock'):
        q=rt.Queue(c).snapshot()
        waiting=[k for k,t in q['tasks'].items() if t['region']==q['active_cube']
                 and t.get('input_status')=='queued' and not t.get('lease')]
        available=120-sum(j['kind']=='cpu' for j in jobs(c,True))
        for _ in range(min(max(0,available),math.ceil(len(waiting)/2))):
            submitted.append(ctl.submit(c,'cpu'))
        atomic(shared/('recovery_submissions_'+str(os.getpid())+'.json'),dict(at=utc(),jobs=submitted,
               queued_months=waiting,global_cpu_cap=120,reason='Replace failed child capacity; retain live sibling workers'))
    print('RECOVERY_JOBS',submitted,flush=True)


if __name__=='__main__':main()
