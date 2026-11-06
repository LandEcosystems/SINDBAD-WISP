"""Audited date-subset migration. Old scientific files are never edited/deleted.

Run only after the old controller and workers have stopped. Input day chunks
are hard-linked into new stores; receipts are explicitly re-keyed, retaining the
original hashes. Normal CPU/verification stages read and verify those arrays.
Completed predictions are copied, not inferred again. Unselected days are absent.
"""
from __future__ import annotations
import argparse
import copy
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from common import atomic, read, sha, now, state_edit, region_config, verify_release, GIB
import catalog

RULE = dict(margin_days=5, margin_after_days=0, partition_parent_margin_days=5)


def new_manifest(old, root, token):
    catalog.selection.validate_manifest(old)
    if old['rules']['margin_days'] != 5 or old['rules'].get('margin_after_days', 5) != 5:
        raise ValueError('Expected the frozen +/-5 parent selection')
    before = old['regions'][token]
    record = copy.deepcopy(before['cube_record'])
    record['coverage_qa'] = str(Path(root)/'regions'/token/'qa/centre_coverage.npz')
    selected = catalog.selection.build_region_selection(token, record, before['fire_dates'],
        audit=before['source_audit'], start=old['rules']['start'], end=old['rules']['end_inclusive'],
        max_chunk_days=old['rules']['max_chunk_days'], **RULE)
    result = catalog.selection.make_manifest([selected], start=old['rules']['start'],
        end=old['rules']['end_inclusive'], max_chunk_days=old['rules']['max_chunk_days'], **RULE)
    if not set(selected['selected_dates']).issubset(before['selected_dates']):
        raise ValueError('New date selection grew rather than shrank')
    return result


def source_root(oldroot, token, entry):
    return Path(entry['final']['source_root']) if entry['status']=='REUSED' else oldroot/'regions'/token


def quiescent(oldroot):
    """Scheduler identity AND persistent stop flags; never infer death from age."""
    oldroot = Path(oldroot)
    if not (oldroot/'state/STOP').exists():
        raise ValueError('Old campaign controller must be stopped first')
    out = subprocess.run(['squeue','-h','-u','ybai','-o','%i|%j'],check=True,
                         capture_output=True,text=True,timeout=30)
    ids = {line.split('|')[0] for line in out.stdout.splitlines()}
    active = sorted(p.stem for p in (oldroot/'state/submissions').glob('*.json') if p.stem in ids)
    if active: raise ValueError('Old campaign still has active jobs: '+','.join(active))
    return dict(checked=now(),old_root=str(oldroot),scheduler_active_jobs=0)


def initialize(root):
    """Freeze all 400 new selections from audited labels; no raw inputs rebuilt."""
    root=Path(root);cfg=read(root/'config/campaign.json');oldroot=Path(cfg['migration_source'])
    verify_release(root);verify_release(oldroot)
    oldstate=read(oldroot/'state/campaign.json')
    entries={}; before_total=after_total=fire_total=0
    for token, entry in sorted(oldstate['regions'].items()):
        rr=root/'regions'/token;oldrr=oldroot/'regions'/token
        old=read(oldrr/'manifests/selection.json');manifest=new_manifest(old,root,token)
        bundle=catalog.bundle_from_manifest(manifest);catalog.freeze_region(bundle,rr,root)
        for folder in ['config','logs','reports','outputs','locks','state','monthly','qa']:
            (rr/folder).mkdir(parents=True,exist_ok=True)
        oldrecord=old['regions'][token]['cube_record'];record=bundle['record']
        stat=Path(record['cube_path']).stat()
        if stat.st_size!=record['cube_stat']['bytes'] or stat.st_mtime_ns!=record['cube_stat']['mtime_ns']:
            raise ValueError('Accepted source cube changed: '+token)
        oldidentity=read(oldrr/'manifests/identity.json')
        if sha(oldrecord['coverage_qa'])!=oldidentity['coverage_qa_sha256']:
            raise ValueError('Parent coverage QA changed')
        shutil.copy2(oldrecord['coverage_qa'],record['coverage_qa'])
        c=read(root/'config/frozen_deployment.json')
        c.update(root=str(rr),scratch=str(Path(cfg['scratch'])/'regions'/token),
                 scratch_budget_bytes=256*GIB,shared_cache_reserve_bytes=8*GIB)
        atomic(rr/'config/deployment.json',c)
        for name in ['deployment.json','source_files.json']:
            shutil.copy2(oldrr/'manifests'/name,rr/'manifests'/name)
        src=source_root(oldroot,token,entry)
        parent=read(src/'manifests/selection.json');catalog.selection.validate_manifest(parent)
        parent_tasks={k:t for k,t in parent['tasks'].items() if t['region']==token}
        parent_day={d:k for k,t in parent_tasks.items() for d in t['dates']}
        mapping={}
        for key,t in manifest['tasks'].items():
            origins={parent_day[d] for d in t['dates']}
            if len(origins)!=1:raise ValueError('New chunk crosses a parent chunk')
            parent_key=next(iter(origins))
            mapping[key]=dict(source_key=parent_key,dates=t['dates'])
        migration=dict(schema=1,source_root=str(src),source_config_sha256=sha(src/'config/deployment.json'),
            source_selection_sha256=sha(src/'manifests/selection.json'),
            source_identity_sha256=sha(src/'manifests/deployment.json'),
            source_final=entry.get('final'),source_status=entry['status'],tasks=mapping,
            rule='observed fire day and preceding five calendar days; no following margin',
            input_window_hours=[-96,24],parent_campaign=str(oldroot))
        atomic(rr/'manifests/pre5_migration.json',migration)
        identity={name+'_sha256':sha(rr/path) for name,path in {
            'config':'config/deployment.json','selection':'manifests/selection.json',
            'regions':'manifests/regions.json','parity_dates':'manifests/parity_dates.json',
            'waves':'manifests/waves.json','pre5_migration':'manifests/pre5_migration.json'}.items()}
        identity.update(deployment_fingerprint=read(rr/'manifests/deployment.json')['fingerprint'],
            coverage_qa_sha256=sha(record['coverage_qa']),campaign_release_sha256=sha(root/'code/release.json'))
        atomic(rr/'manifests/identity.json',identity)
        # Mixed batch tests can be inherited only with the same model/preprocessing.
        gate=src/'reports/mixed_batch_A100.json'
        if gate.exists():
            g=read(gate)
            if not g.get('passed') or g['deployment']!=identity['deployment_fingerprint']:
                raise ValueError('Parent batch gate failed or changed')
            shutil.copy2(gate,rr/'reports/mixed_batch_A100.json')
            atomic(rr/'reports/inherited_batch_gate.json',dict(source=str(gate),sha256=sha(gate)))
        summaries=manifest['summary']
        entries[token]=dict(status='MIGRATING',root=str(rr),region_id=record['region_id'],
            selected_days=summaries['selected_days'],fire_days=summaries['fire_days'],
            episodes=summaries['episodes'],chunks=summaries['tasks'],waves=len(bundle['waves']),
            selection_sha256=manifest['selection_sha256'],source_sha256=record['cube_sha256'],final=None)
        before_total+=old['summary']['selected_days'];after_total+=summaries['selected_days'];fire_total+=summaries['fire_days']
    if len(entries)!=400:raise ValueError('Expected all400 regions including no-fire and completed')
    with state_edit(root) as s:
        s.update(regions=entries,lanes=[None]*10,lane_limit=10,producer_cap=14,
            scan_complete=True,phase='MIGRATING',pilot_passed=True,
            inherited_scaleout_gate=dict(source=str(oldroot/'reports/scaleout_gate.json'),
                sha256=sha(oldroot/'reports/scaleout_gate.json')))
    report=dict(passed=True,regions=len(entries),fire_days=fire_total,
        old_selected_days=before_total,new_selected_days=after_total,
        removed_days=before_total-after_total,rule=RULE,at=now())
    atomic(root/'reports/pre5_selection.json',report);print(json.dumps(report),flush=True)


def identity_for(c, region, dates):
    return dict(deployment=read(Path(c['root'])/'manifests/deployment.json')['fingerprint'],
        region=region['region_id'],lat=region['lat'],lon=region['lon'],dates=dates,cube_sha256=region['cube_sha256'])


def rekey_day(day, date, old_fingerprint, new_fingerprint, source):
    from wisp_batch.common import SHAPES
    if day['date']!=date or day['fingerprint']!=old_fingerprint or set(day['arrays'])!=set(SHAPES):
        raise ValueError('Parent day identity or tensor coverage differs')
    r=copy.deepcopy(day);r['fingerprint']=new_fingerprint
    r.setdefault('metadata',{})['subset_reuse']=dict(source=str(source),source_fingerprint=old_fingerprint,
        source_receipt_sha256=catalog.selection.digest(day),reason='Date-only subset; numerical inputs unchanged')
    return r


def hardlink_day(oldstore, newstore, old_index, new_index):
    """Link immutable compressed chunks; the regular reader verifies decoded hashes."""
    import zarr
    if oldstore.shapes!=newstore.shapes:raise ValueError('Input shapes changed')
    day=read(oldstore.receipt(old_index))
    date=newstore.dates[new_index]
    new_receipt=rekey_day(day,date,oldstore.fingerprint,newstore.fingerprint,oldstore.receipt(old_index))
    if newstore.has(new_index):
        if read(newstore.receipt(new_index))!=new_receipt:raise ValueError('Conflicting imported day')
        return
    for name in newstore.shapes:
        left,right=oldstore.g[name],newstore.g[name]
        if (left.chunks!=right.chunks or left.chunks[0]!=1 or left.dtype!=right.dtype
                or left.compressor!=right.compressor or left.filters!=right.filters
                or left._dimension_separator!='.' or right._dimension_separator!='.'):
            raise ValueError('Input chunk encoding differs')
        pieces=list((oldstore.path/name).glob(str(old_index)+'.*'))
        expected=1
        for size,chunk in zip(left.shape[1:],left.chunks[1:]):expected*= (size+chunk-1)//chunk
        if len(pieces)!=expected:raise ValueError('Committed input is missing compressed pieces')
        for src in pieces:
            if src.is_symlink() or not src.is_file():raise ValueError('Unsafe source chunk')
            dst=newstore.path/name/(str(new_index)+src.name[len(str(old_index)):])
            if dst.exists():
                if sha(dst)!=sha(src):raise ValueError('Interrupted chunk copy differs')
                continue
            try:os.link(src,dst)
            except OSError as e:
                if e.errno!=errno.EXDEV:raise
                shutil.copy2(src,dst)
                if sha(dst)!=sha(src):raise ValueError('Copied compressed input changed')
    atomic(newstore.receipt(new_index),new_receipt)


def verify_imported_prediction(c,key,task):
    """Used by wave validation and cleanup; copied predictions need no input tree."""
    from batch_products import p
    root=Path(c['root']);meta=root/'manifests/tasks'/key
    plan=read(root/'manifests/pre5_migration.json')['tasks'][key]
    receipt=read(meta/'pre5_prediction_reuse.json')
    if (not receipt.get('passed') or receipt['source_key']!=plan['source_key']
            or receipt['dates']!=task['dates'] or task.get('input_status')!='cleaned'
            or task.get('prediction_status')!='done' or task.get('lease')):
        raise ValueError('Invalid imported prediction state')
    if sha(meta/'input_days.json')!=receipt['input_days_sha256']:
        raise ValueError('Imported tensor journal changed')
    days=read(meta/'input_days.json')
    if set(days)!=set(task['dates']) or any(set(d['arrays'])!=p.INPUT_ARRAYS for d in days.values()):
        raise ValueError('Imported tensor journal incomplete')
    output=read(meta/'output.json')
    if output['sha256']!=receipt['output_sha256']:raise ValueError('Imported output identity changed')
    p.validate_chunk(c,key,output,task)
    return receipt


def migrate_region(root, token):
    import numpy as np
    import netCDF4 as nc
    import adapter
    from batch_products import p, empty_result,finalize_region
    from wisp_batch.store import InputStore
    from wisp_batch.common import digest
    from wisp_batch.materialize import channels
    from wisp_stream.outputs import MonthOutput,FIELDS
    root=Path(root);c=region_config(root,token);rr=Path(c['root'])
    adapter.activate(c,root)
    done=rr/'reports/pre5_migrated.json'
    if done.exists():return read(done)
    m=read(rr/'manifests/pre5_migration.json');oldroot=Path(m['source_root'])
    for filename,field in [('config/deployment.json','source_config_sha256'),
                           ('manifests/selection.json','source_selection_sha256'),
                           ('manifests/deployment.json','source_identity_sha256')]:
        if sha(oldroot/filename)!=m[field]:raise ValueError('Parent identity changed')
    oc=read(oldroot/'config/deployment.json')
    oldsel=read(oldroot/'manifests/selection.json');oldreg=read(oldroot/'manifests/regions.json')[token]
    reg=read(rr/'manifests/regions.json')[token]
    for field in ['region_id','cube_sha256','lat','lon']:
        if oldreg[field]!=reg[field]:raise ValueError('Source/grid differs')
    if m['source_identity_sha256']!=sha(rr/'manifests/deployment.json'):
        raise ValueError('Scientific deployment changed')
    q=read(rr/'state/queue.json');linked=predicted=unchecked=0
    final=None;final_dataset=None
    if m['source_status'] in {'COMPLETE','REUSED'}:
        final=p.validate_final(oc,oldreg,oldsel['tasks']);final_dataset=nc.Dataset(final['path'])
        final_dataset.set_auto_maskandscale(False)
        time_index={int(t):i for i,t in enumerate(final_dataset['time'][:])}
    try:
        for key,t in q['tasks'].items():
            oldkey=m['tasks'][key]['source_key'];ot=oldsel['tasks'][oldkey]
            om=oldroot/'manifests/tasks'/oldkey;nm=rr/'manifests/tasks'/key
            old_registration=om/'registration.json'
            if not old_registration.exists():continue
            oi=read(old_registration)['identity'];ni=identity_for(c,reg,t['dates'])
            for field in ['deployment','region','lat','lon','cube_sha256']:
                if oi[field]!=ni[field]:raise ValueError('Parent input identity differs')
            if not set(t['dates']).issubset(oi['dates']):raise ValueError('Imported dates not in parent')
            atomic(nm/'registration.json',dict(key=key,scratch_path=str(Path(c['scratch'])/'inputs'/key),identity=ni))
            if (om/'source_files.json').exists():shutil.copy2(om/'source_files.json',nm/'source_files.json')
            parent_receipt=om/'output.json'
            if final is not None or parent_receipt.exists():
                output=read(parent_receipt)
                if final is None:p.validate_chunk(oc,oldkey,output,ot)
                else:
                    candidates={r['key']:r for r in final['chunk_receipts']}
                    if candidates.get(oldkey)!=output:raise ValueError('Final and chunk receipts disagree')
                olddays=read(om/'input_days.json')
                days={d:rekey_day(olddays[d],d,digest(oi),digest(ni),om/'input_days.json') for d in t['dates']}
                atomic(nm/'input_days.json',days)
                atomic(nm/'input_complete.json',dict(validated=True,fingerprint=digest(ni),days=len(days),
                    imported_prediction_without_materialized_inputs=True,receipt_digest=digest(days)))
                writer=MonthOutput(c,key,reg,t['dates'],digest(ni),output['hardware'])
                src=final_dataset if final_dataset is not None else nc.Dataset(output['path'])
                src.set_auto_maskandscale(False)
                journal=read(om/'prediction_days.json')
                try:
                    for i,d in enumerate(t['dates']):
                        oi_day=ot['dates'].index(d)
                        origin=journal[str(oi_day)]
                        if origin['date']!=d or origin['fingerprint']!=output['fingerprint']:
                            raise ValueError('Source prediction daily journal differs')
                        idx=time_index[int(np.datetime64(d,'s').astype('int64'))] if final_dataset is not None else oi_day
                        for name,h in origin['arrays'].items():
                            if p._array_sha(src[name][idx])!=h:raise ValueError('Source prediction day corrupted')
                        result={name:np.asarray(src[field][idx]) for name,field in FIELDS.items()}
                        writer.write(i,result,dict(reused_prediction=True,source_key=oldkey,
                            source_day=d,source_output_sha256=(final or output)['sha256'],
                            original_timing=origin['timing'],gpu_seconds=0))
                    receipt=writer.finish()
                except BaseException:
                    writer.close();raise
                finally:
                    if final_dataset is None:src.close()
                p.validate_chunk(c,key,receipt,t)
                t.update(input_status='cleaned',prediction_status='done',status='done',reserved=0,
                    input_fingerprint=digest(ni),output_sha256=receipt['sha256'],pre5_reused_prediction=True)
                atomic(nm/'pre5_prediction_reuse.json',dict(passed=True,dates=t['dates'],source_key=oldkey,
                    source_path=(final or output)['path'],source_sha256=(final or output)['sha256'],
                    input_days_sha256=sha(nm/'input_days.json'),output_sha256=receipt['sha256'],at=now()))
                verify_imported_prediction(c,key,t);predicted+=len(t['dates']);continue
            storepath=Path(oc['scratch'])/'inputs'/oldkey
            if not (storepath/'.zgroup').exists():continue
            oldstore=InputStore(storepath)
            if oldstore.fingerprint!=digest(oi):raise ValueError('Input store disagrees with source registration')
            newstore=InputStore(Path(c['scratch'])/'inputs'/key,ni,t['dates'],reg['lat'],reg['lon'],channels(c),create=True)
            for i,d in enumerate(t['dates']):
                j=oldstore.dates.index(d)
                if not oldstore.has(j):continue
                r=read(oldstore.receipt(j));checks=r.get('metadata',{}).get('frozen_checks',{})
                if i==0 and not all(set(checks.get(k,{}))==set(newstore.shapes) and all(checks[k].values())
                                    for k in ['future_input_isolation','frozen_reference_parity']):
                    # A new chunk boundary needs its own original frozen checks.
                    unchecked+=1;continue
                hardlink_day(oldstore,newstore,j,i);linked+=1
            # Queue remains queued: the ordinary builder and wave verifier must
            # decode every linked array and verify its original tensor checksum.
            t['pre5_linked_input_days']=[d for i,d in enumerate(t['dates']) if newstore.has(i)]
            t['reserved']=catalog.task_reservation(t)
        atomic(rr/'state/queue.json',q)
        final_receipt=None;status='READY'
        if not q['tasks']:
            final_receipt=empty_result(c);status='NO_OBSERVED_FIRE'
        elif all(t['prediction_status']=='done' for t in q['tasks'].values()):
            q['phase']='MERGE';atomic(rr/'state/queue.json',q)
            final_receipt=finalize_region(c)
            q.update(phase='COMPLETE',final=final_receipt,completed_at=now());atomic(rr/'state/queue.json',q)
            status='REUSED'
        result=dict(passed=True,token=token,status=status,prediction_days_reused=predicted,
            input_days_linked=linked,new_boundary_days_to_validate=unchecked,at=now(),final=final_receipt)
        atomic(done,result)
        with state_edit(root) as s:s['regions'][token].update(status=status,final=final_receipt,migrated_at=now())
        print('MIGRATED',token,'predictions',predicted,'inputs',linked,status,flush=True)
        return result
    finally:
        if final_dataset is not None:final_dataset.close()


def finish(root):
    root=Path(root);s=read(root/'state/campaign.json')
    reports=[read(root/'regions'/t/'reports/pre5_migrated.json') for t in s['regions']]
    if len(reports)!=400 or not all(r.get('passed') for r in reports):raise ValueError('Incomplete migration')
    if any(e['status']=='MIGRATING' for e in s['regions'].values()):raise ValueError('Unmigrated region')
    result=dict(passed=True,regions=400,at=now(),
        prediction_days_reused=sum(r['prediction_days_reused'] for r in reports),
        input_days_linked=sum(r['input_days_linked'] for r in reports),
        new_boundary_days_to_validate=sum(r['new_boundary_days_to_validate'] for r in reports))
    atomic(root/'reports/pre5_migration_complete.json',result)
    # Resume occupied lanes before admitting fresh regions: imported cache must
    # not sit idle while ten additional regions allocate another cache budget.
    from enroll import prepare_scratch
    oldroot=Path(read(root/'config/campaign.json')['migration_source'])
    lanes=[None]*10
    for lane,token in enumerate(read(oldroot/'state/campaign.json')['lanes']):
        if token and s['regions'][token]['status']=='READY':
            prepare_scratch(root,token);lanes[lane]=token
    with state_edit(root) as s:
        s.update(phase='RUNNING',scan_complete=True,lanes=lanes)
        for lane,token in enumerate(lanes):
            if token:s['regions'][token].update(status='ACTIVE',lane=lane,admitted_at=now(),resumed_parent_lane=True)
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['initialize','region','finish','quiescent'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--token');a=p.parse_args()
    if a.command=='quiescent':print(quiescent(a.root))
    elif a.command=='initialize':initialize(a.root)
    elif a.command=='region':migrate_region(a.root,a.token)
    else:finish(a.root)
