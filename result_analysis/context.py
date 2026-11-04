"""Compact environmental covariates; never used to select successful regions."""
import json
from pathlib import Path
import netCDF4 as nc
import numpy as np
import pandas as pd
from .grouping import get_groups
from .io import atomic_json,file_hash,canonical_hash

def build_context(config,rid):
    root=Path(config['output_root'])/'regions'/rid
    fingerprint=canonical_hash({'completion':json.loads((root/'completion.json').read_text())['fingerprint'],
                                'code':file_hash(__file__)})
    marker=root/'context_complete.json'
    if marker.exists() and json.loads(marker.read_text()).get('fingerprint')==fingerprint:return
    batch=Path(config['batch_root']);g=get_groups(rid,batch/'qa')
    path=batch/'cubes'/(rid+'_frozen_v1')/('daily_master_dev_v5_'+rid+'_frozen_v1.nc')
    rows=[]; maps={};mask=g['run_valid_mask'];ny,nx=mask.shape
    dates=pd.date_range(config['date_start'],config['date_end']);years=np.unique(dates.year)
    with nc.Dataset(path) as ds:
        for var in ('f_rain','f_VPD_day','f_airT'):
            v=ds.variables[var];v.set_auto_mask(False)
            if v.dimensions!=('time','lat','lon'):raise ValueError('Unexpected forcing dimension '+var)
            arr=np.asarray(v[:],dtype=np.float32)
            for y in years:
                sub=arr[dates.year==y]
                value=np.sum(sub,axis=0,dtype=np.float64) if var=='f_rain' else np.mean(sub,axis=0,dtype=np.float64)
                maps[var+'__'+str(y)]=np.where(mask,value,np.nan).astype('f4')
                for group,area in g['group_area_m2'].items():
                    valid=mask&np.isfinite(value)&(area>0);den=area[valid].sum()
                    if not den:continue
                    kind,label=group.split(':',1)
                    rows.append(dict(region_id=rid,year=int(y),variable=var,group_type=kind,group=label,
                                     mean=float(np.sum(value[valid]*area[valid])/den),valid_area_m2=float(den),
                                     complete_period=int((dates.year==y).sum())==(366 if pd.Timestamp(int(y),12,31).is_leap_year else 365),
                                     units='mm per period' if var=='f_rain' else getattr(v,'units','')))
            del arr
    pd.DataFrame(rows).to_csv(root/'environment.csv',index=False)
    np.savez_compressed(root/'environment_maps.npz',**maps)
    atomic_json(marker,dict(status='complete',fingerprint=fingerprint,
                           caveat='Environmental associations are descriptive model-response diagnostics, not observational causal effects.'))
