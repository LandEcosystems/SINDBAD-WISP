"""Audit accepted source cubes and freeze per-region selection independently."""
from __future__ import annotations
import errno
from pathlib import Path
import shutil
import time
from common import atomic, read, sha, now, locked, state_edit, GIB
import catalog

def enroll_one(root, item):
    import numpy as np
    root=Path(root); token=item['token']; rr=root/'regions'/token
    record=catalog.accepted_cube_record(item,rr,verify_checksum=True)
    bundle=catalog.prepare_region(record,verify_checksum=False)
    catalog.freeze_region(bundle,rr,root)
    cfg=read(root/'config/campaign.json')
    for name in ['config','logs','reports','outputs','locks','state','monthly']:
        (rr/name).mkdir(parents=True,exist_ok=True)
    c=read(root/'config/frozen_deployment.json')
    c.update(root=str(rr),scratch=str(Path(cfg['scratch'])/'regions'/token),
             scratch_budget_bytes=256*GIB,shared_cache_reserve_bytes=8*GIB)
    atomic(rr/'config/deployment.json',c)
    for name in ['deployment.json','source_files.json']:
        shutil.copy2(root/'manifests'/name,rr/'manifests'/name)
    qp=Path(record['coverage_qa']);qp.parent.mkdir(parents=True,exist_ok=True)
    temporary=qp.with_suffix('.tmp.npz')
    np.savez_compressed(temporary,**catalog.coverage_arrays(record));temporary.replace(qp)
    identity={name+'_sha256':sha(rr/path) for name,path in {
        'config':'config/deployment.json','selection':'manifests/selection.json',
        'regions':'manifests/regions.json','parity_dates':'manifests/parity_dates.json',
        'waves':'manifests/waves.json'}.items()}
    identity.update(deployment_fingerprint=read(rr/'manifests/deployment.json')['fingerprint'],
                    coverage_qa_sha256=sha(qp),campaign_release_sha256=sha(root/'code/release.json'))
    atomic(rr/'manifests/identity.json',identity)
    # Reuse is explicit and verified; a missing old result never means completion.
    import adapter
    adapter.activate(c,root)
    from batch_products import verify_reuse,empty_result
    receipt=None; status='READY'
    if token in {'T000001','T000830'}:
        receipt=verify_reuse(cfg['pilot_root'],c)
        status='REUSED'
    elif bundle['selected_days']==0:
        receipt=empty_result(c);status='NO_OBSERVED_FIRE'
    atomic(rr/'reports/enrolled.json',dict(passed=True,at=now(),token=token,
          source_sha256=record['cube_sha256'],selection_sha256=bundle['selection']['selection_sha256']))
    return dict(status=status,root=str(rr),region_id=record['region_id'],
        selected_days=bundle['selected_days'],fire_days=bundle['selection']['summary']['fire_days'],
        episodes=bundle['selection']['summary']['episodes'],chunks=len(bundle['selection']['tasks']),
        waves=len(bundle['waves']),selection_sha256=bundle['selection']['selection_sha256'],
        source_sha256=record['cube_sha256'],enrolled_at=now(),final=receipt)

def scan(root):
    root=Path(root); cfg=read(root/'config/campaign.json')
    with locked(root/'locks/scanner.lock',False):
        inv=catalog.inventory(cfg['catalog'],cfg['acceptance'])
        atomic(root/'reports/inventory.json',inv)
        for item in inv['accepted']:
            token=item['token']
            with state_edit(root) as state:
                old=state['regions'][token]
                if old['status'] not in {'WAITING_SOURCE','SCANNING','RETRY_SOURCE'}: continue
                if old.get('retry_after',0)>time.time():continue
                old.update(status='SCANNING')
            try:
                result=enroll_one(root,item)
                with state_edit(root) as state: state['regions'][token].update(result)
                print('ENROLLED',token,result['selected_days'],result['status'],flush=True)
            except Exception as exc:
                import traceback
                transient=isinstance(exc,OSError) and exc.errno in {
                    errno.EIO,errno.ENXIO,errno.ESTALE,errno.ETIMEDOUT,errno.ECONNRESET,errno.ENETUNREACH}
                with state_edit(root) as state:
                    entry=state['regions'][token]; attempts=entry.get('source_attempts',0)+1
                    entry.update(status='RETRY_SOURCE' if transient and attempts<3 else 'BLOCKED',
                                 source_attempts=attempts,error=str(exc),retry_after=time.time()+120)
                atomic(root/'reports/enrollment_errors'/f'{token}.json',dict(
                    at=now(),error=str(exc),transient=transient,traceback=traceback.format_exc()))
                print('ENROLLMENT_ERROR',token,str(exc),flush=True)
        with state_edit(root) as state:
            for item in inv['invalid']:
                state['regions'][item['token']].update(status='BLOCKED',error=item['error'])
            state.update(scan_complete=True,last_scan_at=now(),last_scan_epoch=time.time(),
                         source_counts=inv['counts'])

def prepare_scratch(root,token):
    """Copy checked tiny fire indices into a private region cache at admission."""
    from common import region_config
    root=Path(root);c=region_config(root,token)
    target=Path(c['scratch'])/'cache/fire_index';target.mkdir(parents=True,exist_ok=True)
    manifest=read(root/'manifests/fire_index_source.json')
    for name,checksum in manifest['files'].items():
        source=Path(manifest['directory'])/name;dest=target/name
        if dest.exists():
            if sha(dest)!=checksum:raise ValueError('Private fire index changed: '+str(dest))
            continue
        if sha(source)!=checksum:raise ValueError('Audited index source changed')
        tmp=dest.with_name(dest.name+'.copying');shutil.copy2(source,tmp)
        if sha(tmp)!=checksum:raise ValueError('Private fire index copy differs')
        tmp.replace(dest)
    rr=Path(c['root'])
    for p in target.glob('*.json'):
        atomic(rr/'manifests/fire_indices'/p.name,read(p))
    atomic(rr/'reports/private_cache.json',dict(passed=True,files=manifest['files'],at=now()))
