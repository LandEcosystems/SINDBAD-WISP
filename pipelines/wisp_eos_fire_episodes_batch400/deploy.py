"""Stage a new immutable campaign. Never changes or stops an older campaign."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
import sys
from common import (BASE, LEGACY, PILOT, PYTHON, DEFAULT_ROOT, DEFAULT_SCRATCH,
                    atomic, read, sha, now, GIB, verify_release)

def stage(source, root, scratch, commit, migration_source=None):
    source, root, scratch = map(Path, (source, root, scratch))
    if (root/'code/release.json').exists():
        verify_release(root)
        raise FileExistsError('Existing immutable deployment; use its controller, never overwrite')
    if scratch.exists() and any(scratch.iterdir()):
        raise ValueError('New campaign scratch is not empty')
    for folder in ['code/batch400','code/wisp_eos_fire_episodes','config','manifests',
                   'state/submissions','logs','reports','locks','regions','outputs']:
        (root/folder).mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(scratch).free < 3*1024*GIB:
        raise ValueError('Require at least 3 TiB currently free before ten-lane rollout')
    for src, dest in [(source, root/'code/batch400'),
                       (source.parent/'wisp_eos_fire_episodes', root/'code/wisp_eos_fire_episodes')]:
        for p in src.iterdir():
            if p.is_file() and p.suffix in {'.py','.md'}:
                shutil.copy2(p, dest/p.name)
    catalog = BASE/'shift_v3_60km/sampling/named_production_regions.json'
    shutil.copy2(catalog, root/'manifests/named_production_regions.json')
    records = read(catalog)['regions']
    if len(records)!=400 or len({r['tile_id'] for r in records})!=400:
        raise ValueError('Expected exactly400 unique frozen tiles')
    original = read(LEGACY/'config/frozen_deployment.json')
    original.pop('config_path',None)
    atomic(root/'config/frozen_deployment.json',original)
    shutil.copy2(LEGACY/'manifests/deployment.json',root/'manifests/deployment.json')
    shutil.copy2(LEGACY/'manifests/source_files.json',root/'manifests/source_files.json')
    config = dict(root=str(root),scratch=str(scratch),created=now(),git_commit=commit,
        catalog=str(root/'manifests/named_production_regions.json'),
        acceptance=str(BASE/'shift_v3_60km/reports/acceptance'),
        max_lanes=10,pilot_lanes=2,max_cpu_jobs=320,max_lane_jobs=32,max_producers_per_lane=30,
        cpu_count=4,cpu_memory_gib=16,producers_per_job=2,wall_hours=24,
        partitions='work,small,gpu',region_budget_bytes=256*GIB,wave_budget_bytes=240*GIB,
        max_wave_days=1000,cache_reserve_bytes=8*GIB,global_gpu_jobs=1,batch_size=8,
        cleanup_delay_seconds=600,gpu_idle_seconds=300,scan_refresh_seconds=300,
        legacy_root=str(LEGACY),pilot_root=str(PILOT),existing_model_inference_only=True)
    if migration_source is not None:
        config.update(migration_source=str(Path(migration_source).resolve()),
                      margin_before_days=5, margin_after_days=0)
    atomic(root/'config/campaign.json',config)
    source_index=Path(read(PILOT/'config/deployment.json')['scratch'])/'cache/fire_index'
    hashes={}
    for p in sorted(source_index.glob('*.json')):
        r=read(p); data=p.with_suffix('.parquet')
        if sha(data)!=r['index_sha256'] or data.stat().st_size!=r['index_bytes']:
            raise ValueError('Unverified pilot fire index')
        for f in [p,data]: hashes[f.name]=sha(f)
    if len(hashes)!=24: raise ValueError('Expected twelve audited fire index pairs')
    atomic(root/'manifests/fire_index_source.json',dict(directory=str(source_index),files=hashes))
    tokens={f"T{r['tile_id']:06d}":dict(status='WAITING_SOURCE',region_id=r['region_id']) for r in records}
    atomic(root/'state/campaign.json',dict(schema=1,created=now(),regions=tokens,
          lanes=[None]*10,lane_limit=2,scan_complete=False,phase='SCANNING',pilot_passed=False))
    template=(LEGACY/'code/job.sh').read_text()
    cache=str(scratch/'operational_cache')
    template=template.replace('/Net/Groups/BGI/tscratch/ybai/wisp_eos_batch1_v1/cache',cache)
    launcher=template[:template.rfind('exec ')]+(
        'export WISP_CAMPAIGN_ROOT='+str(root)+'\nexec '+PYTHON+' -u '+
        str(root/'code/batch400/controller.py')+' "$@"\n')
    target=root/'code/run.sh'; target.write_text(launcher); target.chmod(0o775)
    files={str(p.relative_to(root/'code')):sha(p) for p in sorted((root/'code').rglob('*')) if p.is_file()}
    atomic(root/'code/release.json',dict(created=now(),git_commit=commit,files=files,
        campaign_config_sha256=sha(root/'config/campaign.json'),
        catalog_sha256=sha(root/'manifests/named_production_regions.json'),
        source_files={name:sha(root/name) for name in ['config/frozen_deployment.json',
                     'manifests/deployment.json','manifests/source_files.json','manifests/fire_index_source.json']}))
    verify_release(root)
    print('STAGED',root,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--root',type=Path,default=DEFAULT_ROOT);p.add_argument('--scratch',type=Path,default=DEFAULT_SCRATCH)
    p.add_argument('--commit',required=True);p.add_argument('--migration-source',type=Path);a=p.parse_args()
    stage(a.source,a.root,a.scratch,a.commit,a.migration_source)
