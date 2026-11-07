"""Journalled operational upgrade with all workers stopped and GPU held.

Preserves all date selections, inputs, predictions and region queues. It only
changes the controller release, campaign scheduling policy and release bindings.
"""
from pathlib import Path
import argparse
import copy
import json
import os
import shutil
import subprocess

from common import read, atomic, sha, now, locked, verify_release, GIB


def quiescent(root, held_gpu):
    result=subprocess.run(['squeue','-u',os.environ.get('USER','ybai'),'-h','-r','-o','%i|%j|%T'],
                          check=True,text=True,capture_output=True)
    for line in result.stdout.splitlines():
        jid,name,state=line.split('|')
        if not name.startswith('w4e-'):continue
        record=root/'state/submissions'/(jid+'.json')
        if not record.exists():raise ValueError('Unregistered campaign job '+jid)
        if jid!=held_gpu or name!='w4e-gpu' or state!='PENDING':
            raise ValueError('Worker/controller must stop before upgrade: '+line)
    status=subprocess.run(['scontrol','show','job','-o',held_gpu],check=True,text=True,capture_output=True).stdout
    if 'JobState=PENDING' not in status or 'Reason=JobHeldUser' not in status:
        raise ValueError('GPU must be held by its owner during upgrade')
    for p in (root/'regions').glob('*/state/queue.json'):
        if any(t.get('lease') for t in read(p)['tasks'].values()):
            raise ValueError('Live task ownership remains: '+str(p))


def preserved(root):
    paths=[p for p in (root/'regions').glob('*/state/queue.json')]
    for pattern in ['*/config/deployment.json','*/manifests/selection.json',
                    '*/manifests/waves.json','*/manifests/regions.json',
                    '*/manifests/parity_dates.json']:
        paths.extend((root/'regions').glob(pattern))
    return {str(p.relative_to(root)):sha(p) for p in paths}


def upgrade(source,root,commit,held_gpu):
    source,root=Path(source).resolve(),Path(root).resolve()
    op=root/'operations/backlog2x_20261107'
    archive=op/'release_archive'
    with locked(root/'locks/upgrade.lock'), locked(root/'locks/controller.lock',False), locked(root/'locks/submit.lock'):
        quiescent(root,held_gpu)
        journal_path=op/'upgrade.json'
        if not journal_path.exists():
            old=verify_release(root)
            cfg=read(root/'config/campaign.json')
            if shutil.disk_usage(cfg['scratch']).free < 6*1024*GIB:
                raise ValueError('Require 5 TiB budget plus 1 TiB free-space headroom at upgrade')
            state=read(root/'state/campaign.json')
            if len(state['lanes'])!=10 or state['lane_limit']!=10:
                raise ValueError('Expected original ten resident slots')
            archive.mkdir(parents=True,exist_ok=False)
            for folder in ['config','manifests']:
                shutil.copytree(root/folder,archive/folder)
            atomic(archive/'state_before.json',state)
            identities={p.parent.parent.name:read(p) for p in (root/'regions').glob('*/manifests/identity.json')}
            atomic(archive/'identities_before.json',identities)
            oldsha=sha(root/'code/release.json')
            if any(i['campaign_release_sha256']!=oldsha for i in identities.values()):
                raise ValueError('Mixed operational region identities')
            newcfg=dict(cfg,max_lanes=20,max_build_regions=10,max_total_producers=210,
                        backlog_budget_bytes=5*1024*GIB,backlog_min_free_bytes=1024*GIB,io_policy='observe')
            if newcfg['max_lanes']*newcfg['region_budget_bytes']!=newcfg['backlog_budget_bytes']:
                raise ValueError('Resident-region reservation exceeds total budget')
            atomic(archive/'campaign_new.json',newcfg)
            newstate=copy.deepcopy(state)
            newstate.update(lanes=state['lanes']+[None]*10,lane_limit=20,producer_cap=21,
                user_resource_override=dict(at=now(),producer_cap=21,policy='fixed; observe IO',
                    total_producers=210,max_build_regions=10,resident_regions=20))
            atomic(archive/'state_new.json',newstate)
            staged=archive/'staged_code'
            shutil.copytree(root/'code',staged,ignore=shutil.ignore_patterns('__pycache__'))
            for p in source.iterdir():
                if p.is_file() and p.suffix in {'.py','.md'}:shutil.copy2(p,staged/'batch400'/p.name)
            (staged/'release.json').unlink()
            release=dict(old,created=now(),git_commit=commit,
                files={str(p.relative_to(staged)):sha(p) for p in staged.rglob('*') if p.is_file()},
                campaign_config_sha256=sha(archive/'campaign_new.json'),
                operational_upgrade=dict(previous_release_sha256=oldsha,archive=str(archive)))
            atomic(staged/'release.json',release)
            journal=dict(phase='PREPARED',at=now(),old_release=oldsha,new_release=sha(staged/'release.json'),
                held_gpu=held_gpu,preserved=preserved(root),commit=commit)
            atomic(journal_path,journal)
        journal=read(journal_path)
        if journal['held_gpu']!=held_gpu:raise ValueError('Held GPU identity changed')
        for rel,h in journal['preserved'].items():
            if sha(root/rel)!=h:raise ValueError('Scientific/task state changed: '+rel)
        staged,live=archive/'staged_code',root/'code'
        if live.exists() and sha(live/'release.json')==journal['old_release']:
            os.replace(live,archive/'code')
        if not live.exists():
            if sha(staged/'release.json')!=journal['new_release']:raise ValueError('Staged release changed')
            os.replace(staged,live)
        if sha(live/'release.json')!=journal['new_release']:raise ValueError('Unexpected live release')
        atomic(root/'config/campaign.json',read(archive/'campaign_new.json'))
        atomic(root/'state/campaign.json',read(archive/'state_new.json'))
        for token,before in read(archive/'identities_before.json').items():
            path=root/'regions'/token/'manifests/identity.json'
            after=dict(before,campaign_release_sha256=journal['new_release'])
            if read(path) not in (before,after):raise ValueError('Unexpected region identity '+token)
            atomic(path,after)
        jobpath=root/'state/submissions'/(held_gpu+'.json')
        job=read(jobpath)
        job.update(release_sha256=journal['new_release'],operational_rebind=dict(
            previous_release_sha256=journal['old_release'],at=now(),gpu_remained_held=True))
        atomic(jobpath,job)
        verify_release(root)
        for rel,h in journal['preserved'].items():
            if sha(root/rel)!=h:raise ValueError('Scientific/task state changed: '+rel)
        journal.update(phase='COMPLETE',completed=now(),preserved_files=len(journal['preserved']),
                       scientific_inputs_predictions_and_task_states_unchanged=True)
        atomic(journal_path,journal)
        print('UPGRADED',journal['preserved_files'],'preserved files; 20 resident / 10 BUILD / 210 CPU producer cap',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--commit',required=True);p.add_argument('--held-gpu',required=True)
    a=p.parse_args();upgrade(a.source,a.root,a.commit,a.held_gpu)
