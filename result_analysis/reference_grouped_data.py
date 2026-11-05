"""Class-area weights on the already frozen, paired reference supports.

This does not unmix coarse model/reference values into vegetation-specific fluxes.
Forest x pyrome areas are intersected on the source classification, not obtained
by multiplying marginal proportions or assigning a whole cube to a dominant class.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .grouping import get_groups, PYROMES, VEGETATION
from .references import overlap_matrix, weighted_metrics
from .io import atomic_json, canonical_hash, file_hash, source_identity
from .sujan_followup import NAME, SCENARIOS

GROUPS = ([('pyrome', p, 'pyrome:'+p) for p in PYROMES.values()]
          + [('vegetation', v, 'vegetation:'+v) for v in VEGETATION]
          + [('forest_pyrome', p, 'pyrome_vegetation:'+p+':forest') for p in PYROMES.values()])
KEY = ['region_id', 'product', 'variable', 'reference_cell_id']
BOUNDS = ['reference_lat', 'reference_lon', 'reference_lat_min', 'reference_lat_max',
          'reference_lon_min', 'reference_lon_max']
VALUES = SCENARIOS + ['reference']
MIN_GROUP_AREA_M2 = .01  # Ignore numerical boundary slivers, not mixed classes.


def destination(root):
    return Path(root)/NAME/'grouped_references_v1'


def area_name(family, group):
    return family+'__'+group+'_area_m2'


def extract_region(task):
    root, rid, requested, base_hash = task
    root=Path(root);out=destination(root);cache=out/'support_cache_v2';cache.mkdir(exist_ok=True)
    cfg=json.loads((root/'config.json').read_text())
    refs=root/'regions'/rid/'references'
    qa=Path(cfg['qa_root'])/rid
    paths=[refs/'annual_reference_pairs.csv.gz',refs/'reference_group_weights.csv.gz',
           qa/'ownership.npz',qa/'run_masks.npz',qa/'static/static_inputs.npz',
           Path(cfg['source_geometry']),Path(cfg['pyrome_path'])]
    identity=dict(inputs=source_identity(paths),paired_source_sha256=base_hash,
                  code={n:file_hash(Path(__file__).with_name(n)) for n in
                        ['reference_grouped_data.py','grouping.py','references.py']})
    fingerprint=canonical_hash(identity)
    target=cache/(rid+'.parquet');state=cache/(rid+'.json')
    if state.exists() and target.exists():
        saved=json.loads(state.read_text())
        if saved['fingerprint']!=fingerprint:raise ValueError('Changed group source identity: '+rid)
        if saved['output_sha256']!=file_hash(target):raise ValueError('Damaged group cache: '+rid)
        return str(target)
    meta=pd.read_csv(paths[0],usecols=KEY+BOUNDS).drop_duplicates()
    wanted=pd.DataFrame(requested,columns=KEY)
    meta=wanted.merge(meta,on=KEY,how='left',validate='one_to_one')
    assert np.isfinite(meta[BOUNDS]).all().all()
    info=get_groups(rid,cfg['qa_root'],cfg['source_geometry'],cfg['pyrome_path'])
    with np.load(root/'regions'/rid/'spatial.npz') as z:
        np.testing.assert_allclose(info['lat'],z['lat'],rtol=0,atol=1e-10)
        np.testing.assert_allclose(info['lon'],z['lon'],rtol=0,atol=1e-10)
    names=['overall:all']+[g[2] for g in GROUPS]
    group_area=np.stack([info['run_group_area_m2'][n] for n in names],axis=-1)
    geometric=info['cell_area_m2'][...,None]
    fractions=np.divide(group_area,geometric,out=np.zeros_like(group_area),where=geometric>0)
    assert fractions.min()>=0 and fractions.max()<=1+1e-4
    fractions=np.clip(fractions,0,1).reshape(-1,len(names))
    records=[]
    for (product,var),q in meta.groupby(['product','variable'],sort=True):
        latitude=q[['reference_lat','reference_lat_min','reference_lat_max']].drop_duplicates().sort_values('reference_lat')
        longitude=q[['reference_lon','reference_lon_min','reference_lon_max']].drop_duplicates().sort_values('reference_lon')
        assert not latitude.reference_lat.duplicated().any()
        assert not longitude.reference_lon.duplicated().any()
        matrix,support=overlap_matrix(info['lat'],info['lon'],latitude.reference_lat,longitude.reference_lon,
            reference_lat_bounds=latitude[['reference_lat_min','reference_lat_max']].to_numpy(),
            reference_lon_bounds=longitude[['reference_lon_min','reference_lon_max']].to_numpy())
        group_support=matrix@fractions
        indices=(np.searchsorted(latitude.reference_lat,q.reference_lat)*len(longitude)
                 +np.searchsorted(longitude.reference_lon,q.reference_lon))
        part=q.copy();part['reference_support_area_m2']=support[indices]
        part['natural_area_m2']=group_support[indices,0]
        for k,(family,group,_) in enumerate(GROUPS,start=1):
            part[area_name(family,group)]=group_support[indices,k]
        records.append(part)
    result=pd.concat(records,ignore_index=True)
    for family in ['pyrome','vegetation','forest_pyrome']:
        actual=result[[area_name(f,g) for f,g,_ in GROUPS if f==family]].sum(axis=1)
        expected=result[area_name('vegetation','forest')] if family=='forest_pyrome' else result.natural_area_m2
        np.testing.assert_allclose(actual,expected,rtol=1e-9,atol=.01)
    assert (result.natural_area_m2<=result.reference_support_area_m2+1).all()
    # Independently archived broad-group weights must reproduce on these supports.
    old=pd.read_csv(paths[1])
    old=old[old.group_type.isin(['pyrome','vegetation'])]
    old=old.merge(wanted,on=KEY,validate='many_to_one')
    check=old.merge(result,on=KEY,validate='many_to_one')
    maximum=0.
    for (family,group),q in check.groupby(['group_type','group']):
        difference=np.abs(q.group_area_m2-q[area_name(family,group)])
        maximum=max(maximum,float(difference.max()))
        np.testing.assert_allclose(q.group_area_m2,q[area_name(family,group)],rtol=1e-8,atol=.05)
    result.to_parquet(target,index=False)
    atomic_json(state,dict(status='pass',fingerprint=fingerprint,identity=identity,
                          output_sha256=file_hash(target),region_id=rid,reference_supports=len(result),
                          archived_weights_checked=len(check),max_archived_area_difference_m2=maximum))
    print(f'group supports complete: {rid}',flush=True)
    return str(target)


def prepare(root,workers=4):
    root=Path(root);out=destination(root);out.mkdir(parents=True,exist_ok=True)
    source=root/NAME/'reference_annual_pairs_293.csv.gz'
    base=pd.read_csv(source)
    assert not base.duplicated(['product','variable','reference_cell_id','year']).any()
    assert set(base.year)==set(range(2015,2022)) and base.region_id.nunique()==293
    base_hash=file_hash(source)
    units=base[KEY].drop_duplicates()
    tasks=[(str(root),rid,q.values.tolist(),base_hash) for rid,q in units.groupby('region_id')]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        paths=list(pool.map(extract_region,tasks))
    weights=pd.concat([pd.read_parquet(p) for p in paths],ignore_index=True)
    assert not weights.duplicated(KEY).any() and len(weights)==len(units)
    weights.to_parquet(out/'reference_support_group_areas.parquet',index=False)
    pairs=base.merge(weights.drop(columns=['reference_lat','reference_lon']),on=KEY,how='left',validate='many_to_one')
    assert len(pairs)==len(base) and pairs.natural_area_m2.notna().all()
    pairs.to_parquet(out/'grouped_reference_pairs.parquet',index=False)
    # Compact human-readable summaries: class weighting within each region-year;
    # seven annual values then averaged equally, just as in the original figure.
    yearly=[];summaries=[];metrics=[];coverage=[];slivers=[]
    for family,group,_ in GROUPS:
        col=area_name(family,group)
        tiny=pairs[(pairs[col]>0)&(pairs[col]<=MIN_GROUP_AREA_M2)]
        slivers.append(dict(group_type=family,group=group,n_cell_years=len(tiny),
                            omitted_area_year_m2=float(tiny[col].sum())))
        d=pairs[pairs[col]>MIN_GROUP_AREA_M2].copy()
        for (product,var),q in d.groupby(['product','variable']):
            w=q[col].to_numpy()
            meta=dict(group_type=family,group=group,product=product,variable=var)
            support=q[KEY+[col,'reference_support_area_m2']].drop_duplicates(KEY)
            coverage.append(dict(**meta,n_regions=q.region_id.nunique(),n_cell_years=len(q),
                n_reference_supports=len(support),group_area_km2=support[col].sum()/1e6,
                class_share_of_supported_reference_area=support[col].sum()/support.reference_support_area_m2.sum(),
                n_supports_at_least_90pct_class=int((support[col]/support.reference_support_area_m2>=.9).sum())))
            for scenario in SCENARIOS:
                metrics.append(dict(**meta,scenario=scenario,**weighted_metrics(q[scenario],q.reference,w)))
            current=[]
            for (rid,year),v in q.groupby(['region_id','year']):
                rec=dict(**meta,region_id=rid,year=int(year),group_area_m2=float(v[col].sum()),n_cell_years=len(v))
                rec.update({s:float(np.average(v[s],weights=v[col])) for s in VALUES})
                current.append(rec)
            current=pd.DataFrame(current);yearly.extend(current.to_dict('records'))
            for rid,v in current.groupby('region_id'):
                if set(v.year)!=set(range(2015,2022)):continue
                summaries.append(dict(**meta,region_id=rid,n_years=7,mean_group_area_m2=v.group_area_m2.mean(),
                                      **{s:float(v[s].mean()) for s in VALUES}))
    pd.DataFrame(yearly).to_csv(out/'grouped_region_annual.csv',index=False)
    pd.DataFrame(summaries).to_csv(out/'grouped_region_means.csv',index=False)
    pd.DataFrame(metrics).to_csv(out/'grouped_reference_metrics.csv',index=False)
    pd.DataFrame(coverage).to_csv(out/'group_coverage.csv',index=False)
    pd.DataFrame(slivers).to_csv(out/'numerical_boundary_slivers.csv',index=False)
    assert file_hash(source)==base_hash
    atomic_json(out/'data_validation.json',dict(status='pass',regions=293,base_pairs=len(base),
        reference_supports=len(weights),source=str(source),source_sha256=base_hash,
        group_definitions=[dict(family=f,group=g,source_group=n) for f,g,n in GROUPS],
        support_semantics='Class-area-weighted agreement of whole reference cells; not unmixed class-specific fluxes',
        forest_definition='Original 2019 IGBP 1-5 intersected with pyrome on the same source cells',
        minimum_group_area_m2=MIN_GROUP_AREA_M2,
        reference_group_partition_check='pyromes and vegetation each sum to natural area; forest-pyromes sum to forest',
        inputs=[dict(path=str(p),sha256=file_hash(p)) for p in [root/'config.json',Path(__file__)]]))
    print('Grouped comparison data ready:',out,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);parser.add_argument('--workers',type=int,default=4)
    args=parser.parse_args();prepare(args.root,args.workers)
