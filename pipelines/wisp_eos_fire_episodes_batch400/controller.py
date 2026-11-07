"""Ten bounded region lanes, durable waves, one globally coordinated A100."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
from common import (DEFAULT_ROOT,LEGACY,PREFIX,TERMINAL,atomic,read,sha,now,locked,
                    state_edit,verify_release,region_config,producer_target,ready_order,
                    legacy_gpu_handover_ready,GIB)

STOP=False

def slurm_rows():
    r=subprocess.run(['squeue','-h','-r','-u',os.environ.get('USER','ybai'),'-o','%i|%j|%T'],
                     check=True,text=True,capture_output=True,timeout=30)
    return [dict(zip(('id','name','state'),line.split('|'))) for line in r.stdout.splitlines() if line]

def jobs(root,rows=None):
    answer=[]
    for row in slurm_rows() if rows is None else rows:
        if not row['name'].startswith(PREFIX):continue
        p=Path(root)/'state/submissions'/(row['id']+'.json')
        if not p.exists():raise ValueError('Unregistered campaign job: '+row['id'])
        r=read(p)
        if r['name']!=row['name']:raise ValueError('Job registry mismatch')
        answer.append(dict(row,kind=r['kind'],token=r.get('token'),lane=r.get('lane')))
    return answer

def submit(root,kind,token=None,lane=None,dependency=None):
    root=Path(root);cfg=read(root/'config/campaign.json')
    # This lock protects registry creation as well as the scheduler request.
    with locked(root/'locks/submit.lock'):
        current=jobs(root)
        if kind=='control':
            cs=[j for j in current if j['kind']=='control']
            if cs and not (len(cs)==1 and cs[0]['id']==str(dependency)):return None
        elif kind in {'scan','gpu'} and any(j['kind']==kind for j in current):return None
        elif token and kind!='cpu' and any(j['kind']==kind and j['token']==token for j in current):return None
        if kind!='gpu':
            if sum(j['kind']!='gpu' for j in current)>=cfg['max_cpu_jobs']:return None
            if lane is not None and sum(j['lane']==lane and j['kind']!='gpu' for j in current)>=32:return None
            if kind=='cpu' and sum(j['kind']=='cpu' for j in current)>=cfg.get('max_total_producers',300):return None
        def launch():
            cpus,mem,wall={
                'control':(1,'2G','24:00:00'),'scan':(4,'16G','24:00:00'),
                'cpu':(4,'16G','24:00:00'),'verify':(4,'16G','24:00:00'),
                'wave':(2,'8G','24:00:00'),'cleanup':(1,'4G','24:00:00'),
                'final':(2,'8G','24:00:00'),'gpu':(8,'64G','24:00:00')}[kind]
            name=PREFIX+kind
            args=['sbatch','--parsable','--account=science','--qos=normal',
                  '--job-name='+name,'--partition='+('gpu' if kind=='gpu' else cfg['partitions']),
                  '--cpus-per-task='+str(cpus),'--mem='+mem,'--time='+wall,
                  '--chdir='+str(root),'--output='+str(root/'logs'/(kind+'_%j.out')),
                  '--error='+str(root/'logs'/(kind+'_%j.err'))]
            if kind=='gpu':args+=['--gres=gpu:A100:1']
            if dependency:args+=['--dependency=afterany:'+str(dependency)]
            args += [str(root/'code/run.sh'),kind]
            if token:args+=[token]
            r=subprocess.run(args,check=True,text=True,capture_output=True,timeout=45)
            jid=r.stdout.strip().split(';')[0]
            if not jid.isdigit():raise ValueError('Unexpected sbatch response: '+r.stdout)
            atomic(root/'state/submissions'/(jid+'.json'),dict(job=jid,name=name,kind=kind,
                   token=token,lane=lane,args=args,at=now(),release_sha256=sha(root/'code/release.json')))
            print('SUBMITTED',jid,kind,token,flush=True)
            return jid
        if kind=='gpu':
            # Older continuation code only knows its own job names. It must have
            # exhausted both original cubes before our independently named GPU starts.
            with locked(LEGACY/'campaigns/dual120_v1/submit.lock'):
                if not legacy_gpu_handover_ready():return None
                if any(r['name'] in {'wisp1c-gpu','wisp2c-gpu','wispev-gpu',PREFIX+'gpu'} for r in slurm_rows()):return None
                return launch()
        return launch()

def activate(root,token):
    import adapter
    c=region_config(root,token);adapter.activate(c,root)
    return c,adapter

def retire_pending_producers(root,token,current,reason='current wave inputs complete'):
    """Retire only this region's registered, never-started producer allocations.

    Slurm checks the state again server-side, so a job that starts between squeue
    and scancel is not interrupted. Running workers exit at their checkpoints.
    """
    root=Path(root);retired=[];candidates=[]
    for row in current:
        if (row.get('kind')!='cpu' or row.get('token')!=token or row.get('state')!='PENDING'
                or row.get('name')!=PREFIX+'cpu'):continue
        jid=row['id'];record=read(root/'state/submissions'/(jid+'.json'))
        if (record.get('kind')!='cpu' or record.get('token')!=token or
            record.get('name')!=PREFIX+'cpu' or row.get('name')!=record['name']):
            raise ValueError('Refuse to retire unverified producer '+jid)
        candidates.append(jid)
    receipt=root/'reports/job_retirements'/(str(time.time_ns())+'.json')
    for jid in candidates:
        subprocess.run(['scancel','--state=PENDING',jid],check=True,text=True,
                       capture_output=True,timeout=30)
        retired.append(jid)
        atomic(receipt,
               dict(at=now(),token=token,jobs=retired,reason=reason,
                    scheduler_state_filter='PENDING'))
    return retired

def cpu_parent(root,token):
    import psutil
    c,ad=activate(root,token)
    from wisp_stream.backend import qualify
    rr=Path(c['root']);jid=os.environ.get('SLURM_JOB_ID','local')
    try:
        qualification=qualify(c)
        atomic(rr/'reports/qualification'/(jid+'.json'),qualification)
    except Exception as exc:
        # No task lease exists yet; persist a stage failure so startup errors
        # cannot cause unbounded replacement jobs.
        record_failure(root,token,'cpu-startup',exc)
        raise
    atomic(rr/'state/job_resources'/(jid+'.json'),dict(job=jid,at=now(),
        max_aggregate_rss_bytes=0,current_aggregate_rss_bytes=0,elapsed_seconds=0,
        cpu_seconds=0,allocated_cpu=4,allocated_memory_gib=16,children=[],resource_error=False))
    children=[subprocess.Popen([sys.executable,'-u',str(Path(__file__)),'cpu-one',token]) for _ in range(2)]
    peak=0;times={};start=time.monotonic();sent=False
    while any(p.poll() is None for p in children):
        if (STOP or ad.rt.STOP) and not sent:
            for p in children:
                if p.poll() is None:p.send_signal(signal.SIGTERM)
            sent=True
        rss=0
        for p in [psutil.Process()]+psutil.Process().children(recursive=True):
            try:
                rss+=p.memory_info().rss;t=p.cpu_times();times[p.pid]=t.user+t.system
            except (psutil.NoSuchProcess,psutil.AccessDenied):pass
        peak=max(peak,rss)
        atomic(rr/'state/job_resources'/(jid+'.json'),dict(job=jid,at=now(),
               max_aggregate_rss_bytes=peak,current_aggregate_rss_bytes=rss,
               elapsed_seconds=time.monotonic()-start,cpu_seconds=sum(times.values()),
               allocated_cpu=4,allocated_memory_gib=16,children=[p.pid for p in children],
               resource_error=any(p.poll() not in (None,0) for p in children)))
        time.sleep(10)
    codes=[p.wait() for p in children]
    p=rr/'state/job_resources'/(jid+'.json');r=read(p)
    r.update(completed=now(),child_exit_codes=codes,resource_error=any(codes));atomic(p,r)
    if any(codes):raise RuntimeError('Producer process failed: '+str(codes))

def phase_job(root,token,kind):
    c,ad=activate(root,token);q=ad.Queue(c)
    from batch_products import verify_wave_outputs,cleanup_wave,finalize_region
    if kind=='verify':ad.verify_wave(c)
    elif kind=='wave':
        receipt=verify_wave_outputs(c)
        with q.edit() as s:
            if s['phase']!='WAVE_VERIFY':raise ValueError('Wrong wave verification phase')
            s.update(phase='CLEANUP_WAIT',cleanup_authorized=True,
                     cleanup_not_before_epoch=receipt['completed_epoch']+600)
    elif kind=='cleanup':
        result=cleanup_wave(c)
        if result.get('pending'):return
        with q.edit() as s:
            wave=s['waves'][s['active_wave']]
            if any(s['tasks'][k]['input_status']!='cleaned' for k in wave['keys']):
                raise ValueError('Wave cleanup incomplete')
            if s['active_wave']+1<len(s['waves']):
                s.update(active_wave=s['active_wave']+1,phase='BUILD',cleanup_authorized=False)
                s.pop('cleanup_not_before_epoch',None)
            else:s.update(phase='MERGE',cleanup_authorized=False)
    elif kind=='final':
        receipt=finalize_region(c)
        with q.edit() as s:s.update(phase='COMPLETE',completed_at=now(),final=receipt)

def ready_gpu_regions(root):
    state=read(Path(root)/'state/campaign.json');ready=[]
    for token in state['lanes']:
        if not token:continue
        p=Path(root)/'regions'/token/'state/queue.json'
        s=read(p)
        if s['phase']=='PREDICT' and any(s['tasks'][k]['prediction_status']!='done'
                for k in s['waves'][s['active_wave']]['keys']):
            ready.append((s.get('inputs_verified_at',''),token))
    return [t for _,t in sorted(ready)]

def gpu(root):
    import torch
    if 'A100' not in torch.cuda.get_device_name(0):raise ValueError('A100 required')
    model=None;idle_since=time.monotonic();started=time.monotonic()
    with locked(Path(root)/'locks/gpu_consumer.lock',False):
        while not STOP and time.monotonic()-started<23*3600:
            ready=ready_gpu_regions(root)
            if not ready:
                if time.monotonic()-idle_since>=300:return
                time.sleep(10);continue
            token=ready[0];c,ad=activate(root,token)
            try:
                model=ad.gpu(c,model=model)
                if ad.rt.STOP:return
                q=ad.Queue(c)
                with q.edit() as s:
                    keys=s['waves'][s['active_wave']]['keys']
                    if all(s['tasks'][k]['prediction_status']=='done' and not s['tasks'][k].get('lease') for k in keys):
                        s['phase']='WAVE_VERIFY'
                idle_since=time.monotonic()
            except Exception as exc:
                record_failure(root,token,'gpu',exc)
                # A device failure must not contaminate subsequent regions.
                raise

def pilot_gate(root,state):
    if state.get('pilot_passed'):return True
    tokens=state.get('pilot_tokens',[])
    if any(state['regions'].get(t,{}).get('status')=='BLOCKED' for t in tokens):
        raise ValueError('Initial two-region qualification is blocked; repair that region before expansion')
    if len(tokens)>2:raise ValueError('Qualification cohort must contain exactly two regions')
    if len(tokens)!=2:return False
    report={}
    for token in tokens:
        rr=Path(root)/'regions'/token
        s=read(rr/'state/queue.json')
        complete=[t for t in s['tasks'].values() if t['input_status'] in {'ready','cleaned'}]
        parity=[read(p) for p in (rr/'reports/input_parity').glob('*.json')]
        planned=read(rr/'manifests/parity_dates.json')[token]
        if len(complete)<min(2,len(s['tasks'])) or not all(any(p.get('passed') and p.get('date')==d for p in parity) for d in planned):
            return False
        resources=[read(p) for p in (rr/'state/job_resources').glob('*.json')]
        if not resources:return False
        if any(r.get('max_aggregate_rss_bytes',0)>12*GIB for r in resources):
            raise ValueError('Two-lane resource qualification failed')
        # A recovered I/O failure is retained in audit history; it must not
        # permanently poison a subsequently verified qualification cohort.
        healthy=[r for r in resources if not r.get('resource_error') and
                 r.get('max_aggregate_rss_bytes',0)>0]
        if not healthy:return False
        report[token]=dict(verified_chunks=len(complete),parity_dates=planned,
                           peak_job_bytes=max(r['max_aggregate_rss_bytes'] for r in resources))
    atomic(Path(root)/'reports/scaleout_gate.json',dict(passed=True,at=now(),regions=report,
           release_sha256=sha(Path(root)/'code/release.json')))
    return True

def performance(root,tokens):
    """Read bounded recent timing journals; never interpret queue time as CPU work."""
    end=time.time();events=[]
    for token in tokens:
        if not token:continue
        for p in (Path(root)/'regions'/token/'logs/worker_events').glob('cpu_*.jsonl'):
            if p.stat().st_mtime<end-3600:continue
            for line in p.read_text().splitlines():
                try:r=json.loads(line)
                except json.JSONDecodeError:continue
                if r.get('time',0)>=end-3600 and r.get('date'):events.append(r)
    bins=[]
    for lower,upper in [(end-3600,end-1800),(end-1800,end)]:
        rows=[e for e in events if lower<=e['time']<upper]
        reads=[e.get('read_seconds',e.get('read_s',0)) for e in rows]
        reads=sorted(x for x in reads if x>0)
        unique={(r['task'],r['date']) for r in rows}
        bins.append(dict(days=len(unique),median_read_seconds=reads[len(reads)//2] if reads else None))
    a,b=bins
    pressure=(a['days']>=20 and b['days']>=20 and a['median_read_seconds'] and
              b['median_read_seconds'] and b['median_read_seconds']>2*a['median_read_seconds'] and b['days']<a['days'])
    return dict(at=now(),previous_30min=a,last_30min=b,io_pressure=bool(pressure))

def admit_more_inputs(root, state, cfg):
    """Separate resident prediction backlog from concurrently building regions.

    Waiting for a GPU retains its region cache slot, but no longer consumes
    one of the CPU-building slots. Resident slots bound the conservative total
    cache reservation; the filesystem guard also covers unrelated users' writes.
    """
    if 'max_build_regions' not in cfg:
        return True
    building = 0
    for token in state['lanes']:
        if token:
            queue = read(Path(root)/'regions'/token/'state/queue.json')
            building += queue['phase']=='BUILD'
    if building >= cfg['max_build_regions']:
        return False
    free = shutil.disk_usage(cfg['scratch']).free
    return free >= cfg['region_budget_bytes'] + cfg.get('backlog_min_free_bytes',0)

def record_failure(root,token,kind,exc):
    import errno,traceback
    transient=isinstance(exc,OSError) and exc.errno in {
        errno.EIO,errno.ENXIO,errno.ESTALE,errno.ETIMEDOUT,errno.ECONNRESET,errno.ENETUNREACH}
    folder=Path(root)/('regions/'+token if token else '')
    report=dict(at=now(),kind=kind,token=token,error=str(exc),transient=transient,
                traceback=traceback.format_exc())
    atomic(folder/'reports/errors'/(kind+'_'+str(time.time_ns())+'.json'),report)
    if token and kind not in {'cpu','cpu-one'}:
        with locked(folder/'locks/stage_failures.lock'):
            history_path=folder/'state/stage_failures.json'
            history=read(history_path) if history_path.exists() else {}
            queue=read(folder/'state/queue.json')
            failure_key=str(queue.get('active_wave',0))+':'+kind
            history[failure_key]=history.get(failure_key,0)+1
            atomic(history_path,history)
            report.update(attempts=history[failure_key],retry_after=time.time()+120)
            atomic(folder/'reports/stage_error.json',report)

def report_status(root,current):
    root=Path(root);state=read(root/'state/campaign.json');counts=Counter(r['status'] for r in state['regions'].values())
    details={};completed_days=0;input_days=0
    for token,entry in state['regions'].items():
        if entry['status'] in TERMINAL:
            completed_days+=entry.get('selected_days',0);continue
        if entry['status']!='ACTIVE':continue
        s=read(root/'regions'/token/'state/queue.json')
        done=sum(len(t['dates']) for t in s['tasks'].values() if t['prediction_status']=='done')
        built=sum(len(t['dates']) for t in s['tasks'].values() if t['input_status'] in {'ready','cleaned'})
        # Worker journals count dates in partially finished chunks too.
        fresh=set()
        for p in (root/'regions'/token/'state/workers').glob('cpu_*.json'):
            r=read(p)
            fresh.update((x['task'],x['date']) for x in r.get('fresh_days',[]))
        built=max(built,len(fresh));input_days+=built;completed_days+=done
        details[token]=dict(phase=s['phase'],wave=s['active_wave']+1,waves=len(s['waves']),
            total_days=entry['selected_days'],input_days=built,predicted_days=done,
            reserved_gib=sum(t.get('reserved',0) for t in s['tasks'].values())/GIB,
            cache_peak_gib=s.get('scratch_peak_measured_bytes',0)/GIB,
            admission_pause=s.get('admission_pause'))
    r=dict(at=now(),phase=state['phase'],regions=dict(counts),active=details,
        jobs=current,cpu_jobs=sum(j['kind']!='gpu' for j in current),gpu_jobs=sum(j['kind']=='gpu' for j in current),
        lane_limit=state['lane_limit'],producer_cap=state.get('producer_cap',30),
        selected_days_known=sum(r.get('selected_days',0) for r in state['regions'].values()),
        predicted_days=completed_days,active_input_days=input_days,
        blocked={t:r.get('error') for t,r in state['regions'].items() if r['status']=='BLOCKED'})
    atomic(root/'reports/progress.json',r)
    outputs=[dict(token=t,region_id=e['region_id'],status=e['status'],selected_days=e.get('selected_days'),
                  final=e.get('final')) for t,e in state['regions'].items()]
    atomic(root/'outputs/catalog.json',dict(at=now(),regions=outputs))
    print(json.dumps({k:v for k,v in r.items() if k not in {'jobs','active','blocked'}}),flush=True)
    return r

def control(root,once=False):
    root=Path(root);start=time.monotonic();last_perf=0
    with locked(root/'locks/controller.lock',False):
        while not STOP and time.monotonic()-start<22*3600 and not (root/'state/STOP').exists():
            current=jobs(root);state=read(root/'state/campaign.json')
            need_scan=(not state['scan_complete'] or any(r['status'] in {'WAITING_SOURCE','RETRY_SOURCE','SCANNING'} for r in state['regions'].values()))
            if need_scan and time.time()-state.get('last_scan_epoch',0)>=300:
                submit(root,'scan')
            if state['scan_complete']:
                cfg=read(root/'config/campaign.json')
                # A cleaned wave may return to BUILD while ten other regions
                # are already building. Do not interrupt current owners; admit
                # producer jobs for at most the configured number of regions.
                build_tokens=[t for t in state['lanes'] if t and
                    read(root/'regions'/t/'state/queue.json')['phase']=='BUILD']
                working={j['token'] for j in current if j['kind']=='cpu'}
                build_order=sorted(build_tokens,key=lambda t:(t not in working,state['lanes'].index(t)))
                admitted_builds=set(build_order[:cfg.get('max_build_regions',len(build_order))])
                # Advance only regions whose workers have all relinquished this lane.
                for lane,token in enumerate(state['lanes']):
                    if not token:continue
                    c,ad=activate(root,token);q=ad.Queue(c)
                    if ad.rt.STOP:return
                    ad.recover(c,{r['id'] for r in slurm_rows()})
                    s=q.snapshot();rr=Path(c['root'])
                    err=rr/'reports/stage_error.json'
                    if err.exists():
                        e=read(err)
                        if e.get('transient') and e.get('attempts',0)<3 and time.time()>=e.get('retry_after',0):
                            err.rename(rr/'reports/errors'/('retried_stage_'+str(time.time_ns())+'.json'))
                        elif not e.get('transient') or e.get('attempts',0)>=3:
                            with state_edit(root) as d:d['regions'][token].update(status='BLOCKED',error=e['error'])
                            (rr/'state/STOP').touch()
                            retire_pending_producers(root,token,current,reason='region blocked by stage error')
                            continue
                        else:continue
                    blocked=[t for t in s['tasks'].values() if 'blocked' in (t['input_status'],t['prediction_status'])]
                    if any(j['kind']=='gpu' for j in current) and any(
                            t['prediction_status']=='blocked' for t in blocked):
                        # The GPU wrapper annotates the frozen worker's error
                        # with classification/counters before its job exits.
                        continue
                    permanent=[t for t in blocked if not t.get('transient') or t.get('attempts',0)>=3]
                    if permanent:
                        with state_edit(root) as d:d['regions'][token].update(status='BLOCKED',error=permanent[0].get('error'))
                        (rr/'state/STOP').touch()
                        retire_pending_producers(root,token,current,reason='region blocked by task error')
                        continue
                    if blocked:continue
                    own=[j for j in current if j['token']==token]
                    # Pending workers have never opened inputs. Do not wait for
                    # empty jobs to obtain resources after the work is done.
                    if s['phase']!='BUILD' or all(
                            t['input_status'] in {'ready','cleaned'} and not t.get('lease')
                            for t in (s['tasks'][k] for k in s['waves'][s['active_wave']]['keys'])):
                        retire_pending_producers(root,token,own)
                    if s['phase']=='COMPLETE' and not own:
                        with state_edit(root) as d:
                            d['regions'][token].update(status='COMPLETE',final=s['final'],completed_at=now())
                            d['lanes'][lane]=None
                        continue
                    if not s['waves']:raise ValueError('Active region has no waves')
                    keys=s['waves'][s['active_wave']]['keys'];tasks=[s['tasks'][k] for k in keys]
                    if s['phase']=='BUILD':
                        if all(t['input_status'] in {'ready','cleaned'} and not t.get('lease') for t in tasks):
                            with q.edit() as d:d['phase']='INPUT_VERIFY'
                        elif not own or any(j['kind']=='cpu' for j in own):
                            if token not in admitted_builds:continue
                            cap=state.get('producer_cap',30) if state.get('pilot_passed') else 2
                            workers=sorted(j['id'] for j in own if j['kind']=='cpu')
                            with q.edit() as d:
                                if len(workers)>cap:d['allowed_cpu_jobs']=workers[:cap]
                                else:d.pop('allowed_cpu_jobs',None)
                            pending=sum(t['input_status'] in {'queued','building'} for t in tasks)
                            for _ in range(producer_target(pending,sum(j['kind']=='cpu' for j in own),cap)):
                                if submit(root,'cpu',token,lane) is None:break
                    elif s['phase']=='PREDICT':
                        if not any(j['kind']=='gpu' for j in current) and all(t['prediction_status']=='done' and not t.get('lease') for t in tasks):
                            with q.edit() as d:d['phase']='WAVE_VERIFY'
                    elif s['phase'] in {'INPUT_VERIFY','WAVE_VERIFY','CLEANUP_WAIT','MERGE'}:
                        kind={'INPUT_VERIFY':'verify','WAVE_VERIFY':'wave','CLEANUP_WAIT':'cleanup','MERGE':'final'}[s['phase']]
                        if kind=='cleanup' and time.time()<s.get('cleanup_not_before_epoch',math.inf):continue
                        if not own:submit(root,kind,token,lane)
                state=read(root/'state/campaign.json')
                if not state.get('pilot_passed'):
                    try:qualified=pilot_gate(root,state)
                    except ValueError as exc:
                        with state_edit(root) as d:d.update(phase='PILOT_BLOCKED',pilot_error=str(exc))
                        atomic(root/'reports/scaleout_gate.json',dict(passed=False,at=now(),error=str(exc)))
                        report_status(root,jobs(root));return
                    if qualified:
                        with state_edit(root) as d:d.update(pilot_passed=True,lane_limit=10,phase='RUNNING',expanded_at=now())
                        state=read(root/'state/campaign.json')
                # Release a blocked lane only after all its tasks actually stop.
                for lane,token in enumerate(state['lanes']):
                    if token and state['regions'][token]['status']=='BLOCKED' and not any(j['token']==token for j in jobs(root)):
                        with state_edit(root) as d:d['lanes'][lane]=None
                state=read(root/'state/campaign.json')
                for lane in range(state['lane_limit']):
                    if state['lanes'][lane]:continue
                    if not admit_more_inputs(root,state,cfg):break
                    pending=ready_order(state['regions'])
                    if not pending:break
                    token=pending[0]
                    from enroll import prepare_scratch
                    prepare_scratch(root,token)
                    with state_edit(root) as d:
                        if d['lanes'][lane] or d['regions'][token]['status']!='READY':raise ValueError('Region admission raced')
                        d['lanes'][lane]=token;d['regions'][token].update(status='ACTIVE',lane=lane,admitted_at=now())
                        if not d.get('pilot_passed'):
                            d.setdefault('pilot_tokens',[])
                            if token not in d['pilot_tokens']:d['pilot_tokens'].append(token)
                    state=read(root/'state/campaign.json')
                if ready_gpu_regions(root):submit(root,'gpu')
                if state.get('pilot_passed') and time.time()-last_perf>=300:
                    perf=performance(root,state['lanes']);atomic(root/'reports/io_monitor.json',perf)
                    if perf['io_pressure'] and cfg.get('io_policy','adaptive')=='adaptive':
                        with state_edit(root) as d:
                            d['producer_cap']=max(2,d.get('producer_cap',30)//2)
                            d['resource_adjustment']=perf
                    last_perf=time.time()
            state=read(root/'state/campaign.json')
            if all(e['status'] in TERMINAL for e in state['regions'].values()):
                with state_edit(root) as d:d.update(phase='COMPLETE',completed_at=now())
                report_status(root,jobs(root));return
            if state['scan_complete'] and not any(e['status'] in {'READY','ACTIVE','WAITING_SOURCE','RETRY_SOURCE','SCANNING'} for e in state['regions'].values()):
                with state_edit(root) as d:d.update(phase='ATTENTION_REQUIRED')
                report_status(root,jobs(root));return
            report_status(root,jobs(root))
            if once:return
            time.sleep(30)
        if not STOP and not (root/'state/STOP').exists():
            if not submit(root,'control',dependency=os.environ.get('SLURM_JOB_ID')):
                raise RuntimeError('Controller renewal was not admitted')

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['submit-control','control','once','scan','cpu','cpu-one','verify','wave','cleanup','final','gpu','status','migrate-init','migrate-region','migrate-finish'])
    p.add_argument('token',nargs='?');p.add_argument('--root',type=Path,default=Path(os.environ.get('WISP_CAMPAIGN_ROOT',DEFAULT_ROOT)))
    a=p.parse_args();root=a.root;verify_release(root)
    def stop(sig,frame):
        global STOP
        STOP=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        if a.command.startswith('migrate-'):
            import pre5_migration as migration
            migration.quiescent(read(root/'config/campaign.json')['migration_source'])
            if a.command=='migrate-init':migration.initialize(root)
            elif a.command=='migrate-region':migration.migrate_region(root,a.token)
            else:migration.finish(root)
        elif a.command=='submit-control':submit(root,'control')
        elif a.command in {'control','once'}:
            for attempt in range(3):
                try:
                    control(root,a.command=='once');break
                except (OSError,subprocess.TimeoutExpired,subprocess.CalledProcessError) as exc:
                    record_failure(root,None,a.command,exc)
                    if attempt==2:raise
                    time.sleep(30)
        elif a.command=='scan':
            from enroll import scan
            scan(root)
        elif a.command=='cpu':cpu_parent(root,a.token)
        elif a.command=='cpu-one':
            c,ad=activate(root,a.token);ad.cpu_one(c)
        elif a.command in {'verify','wave','cleanup','final'}:phase_job(root,a.token,a.command)
        elif a.command=='gpu':gpu(root)
        else:report_status(root,jobs(root))
    except Exception as exc:
        record_failure(root,a.token,a.command,exc)
        raise

if __name__=='__main__':main()
