"""Quiescent, journalled switch to actual-input accounting and CPU/GPU consumers."""
from pathlib import Path
import argparse
import copy
import os
import shutil
import subprocess
from common import read,atomic,sha,now,locked,verify_release,GIB
from backlog_upgrade import preserved


def quiescent(root):
    rows=subprocess.run(['squeue','-u',os.environ.get('USER','ybai'),'-h','-r','-o','%i|%j|%T|%R'],
        check=True,text=True,capture_output=True,timeout=30).stdout
    held=[]
    for line in rows.splitlines():
        jid,name,state,reason=line.split('|',3)
        if not name.startswith('w4e-'):continue
        path=root/'state/submissions'/(jid+'.json')
        if not path.exists():raise ValueError('Unregistered job '+jid)
        record=read(path)
        if record['name']!=name or record['kind']=='control':raise ValueError('Stop controller before upgrade')
        if state!='PENDING' or 'JobHeldUser' not in reason:raise ValueError('All jobs must finish or be held: '+line)
        held.append(jid)
    for p in (root/'regions').glob('*/state/queue.json'):
        if any(t.get('lease') for t in read(p)['tasks'].values()):raise ValueError('Task owner still active '+str(p))
    return held


def upgrade(source,root,commit):
    source,root=Path(source).resolve(),Path(root).resolve()
    op=root/'operations/actual5tb_cpu_gpu_20261108';archive=op/'release_archive'
    with locked(root/'locks/upgrade.lock'),locked(root/'locks/controller.lock',False),locked(root/'locks/submit.lock'):
        held=quiescent(root);journal_path=op/'upgrade.json'
        if not journal_path.exists():
            old=verify_release(root);cfg=read(root/'config/campaign.json');state=read(root/'state/campaign.json')
            archive.mkdir(parents=True,exist_ok=False)
            for folder in ['config','manifests']:shutil.copytree(root/folder,archive/folder)
            atomic(archive/'state_before.json',state)
            identities={p.parent.parent.name:read(p) for p in (root/'regions').glob('*/manifests/identity.json')}
            atomic(archive/'identities_before.json',identities)
            oldsha=sha(root/'code/release.json')
            if any(i['campaign_release_sha256']!=oldsha for i in identities.values()):raise ValueError('Mixed release identities')
            newcfg=dict(cfg,input_budget_mode='actual',backlog_budget_bytes=5_000_000_000_000,
                input_budget_scan_seconds=3600,input_write_headroom_bytes=2*GIB,
                max_lanes=len(state['regions']),max_build_regions=10,max_total_producers=210,
                max_cpu_consumers=8,cpu_prediction_precision='float32',cpu_prediction_batch_size=1,
                io_policy='observe',inference_backends=['cuda','cpu'],git_commit=commit)
            atomic(archive/'campaign_new.json',newcfg)
            newstate=copy.deepcopy(state)
            newstate.update(lanes=state['lanes']+[None]*(newcfg['max_lanes']-len(state['lanes'])),
                lane_limit=newcfg['max_lanes'],producer_cap=21,
                user_resource_override=dict(at=now(),policy='actual input disk <=5 decimal TB; CPU and GPU prediction',
                    producer_cap=21,total_producers=210,max_build_regions=10,max_cpu_consumers=8))
            atomic(archive/'state_new.json',newstate)
            staged=archive/'staged_code'
            shutil.copytree(root/'code',staged,ignore=shutil.ignore_patterns('__pycache__'))
            for p in source.iterdir():
                if p.is_file() and p.suffix in {'.py','.md'}:shutil.copy2(p,staged/'batch400'/p.name)
            (staged/'release.json').unlink()
            release=dict(old,created=now(),git_commit=commit,
                files={str(p.relative_to(staged)):sha(p) for p in staged.rglob('*') if p.is_file()},
                campaign_config_sha256=sha(archive/'campaign_new.json'),
                operational_upgrade=dict(previous_release_sha256=oldsha,archive=str(archive),
                    CPU_float32_formal_outputs_authorized=True))
            atomic(staged/'release.json',release)
            atomic(journal_path,dict(phase='PREPARED',at=now(),commit=commit,held_jobs=held,
                old_release=oldsha,new_release=sha(staged/'release.json'),preserved=preserved(root)))
        journal=read(journal_path)
        if set(held)!=set(journal['held_jobs']):raise ValueError('Held scheduler set changed')
        for rel,digest in journal['preserved'].items():
            if sha(root/rel)!=digest:raise ValueError('Input/task state changed '+rel)
        live=root/'code';staged=archive/'staged_code'
        if live.exists() and sha(live/'release.json')==journal['old_release']:os.replace(live,archive/'code')
        if not live.exists():
            if sha(staged/'release.json')!=journal['new_release']:raise ValueError('Staged identity changed')
            os.replace(staged,live)
        if sha(live/'release.json')!=journal['new_release']:raise ValueError('Unexpected live release')
        atomic(root/'config/campaign.json',read(archive/'campaign_new.json'))
        atomic(root/'state/campaign.json',read(archive/'state_new.json'))
        for token,before in read(archive/'identities_before.json').items():
            path=root/'regions'/token/'manifests/identity.json'
            after=dict(before,campaign_release_sha256=journal['new_release'])
            if read(path) not in (before,after):raise ValueError('Region identity changed '+token)
            atomic(path,after)
        for jid in held:
            path=root/'state/submissions'/(jid+'.json');record=read(path)
            record.update(release_sha256=journal['new_release'],operational_rebind=dict(
                previous_release_sha256=journal['old_release'],at=now(),remained_held=True))
            atomic(path,record)
        verify_release(root)
        for rel,digest in journal['preserved'].items():
            if sha(root/rel)!=digest:raise ValueError('Input/task state changed '+rel)
        journal.update(phase='COMPLETE',completed=now(),preserved_files=len(journal['preserved']))
        atomic(journal_path,journal)
        print('UPGRADED actual5TB; CPU/GPU enabled; preserved',len(journal['preserved']),'files; held jobs',len(held),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path)
    p.add_argument('--source',type=Path,default=Path(__file__).resolve().parent);p.add_argument('--commit',required=True)
    a=p.parse_args();upgrade(a.source,a.root,a.commit)
