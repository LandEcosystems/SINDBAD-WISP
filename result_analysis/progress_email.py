"""Evidence and an unsent, illustrated progress email; no model or scheduler writes."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
from email.message import EmailMessage
from email.policy import SMTP
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import numpy as np
import pandas as pd

from .email_case_studies import PORTUGAL, SOURCE_ROOT, annual, series
from .findings_report import snapshot, destination
from .io import file_hash

BATCH=Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/shift_v3_60km')


def output(root):
    p=destination(root)/'email_update_20261105_v2'
    p.mkdir(parents=True,exist_ok=True)
    return p


def audit(root):
    root=Path(root);out=output(root);now=dt.datetime.now(dt.timezone.utc);prepared=snapshot(root)
    states=[json.loads(p.read_text()) for p in (BATCH/'state/regions').glob('*.json')]
    assert len(states)==400
    complete=[q for q in states if q['status']=='complete']
    for q in complete:
        assert Path(q['final_path']).is_file()
        assert (BATCH/'reports/acceptance'/(q['region_id']+'.json')).is_file()
    estimates=[]
    for q in states:
        if q['status']=='complete':continue
        records=[]
        for line in (BATCH/'logs'/(q['region_id']+'.jsonl')).read_text().splitlines():
            try:x=json.loads(line)
            except ValueError:continue
            if x.get('stage')=='weather' and 'months_complete' in x and 'time' in x:
                t=dt.datetime.fromisoformat(x['time'])
                if (now-t).total_seconds()<6*3600:records.append(x)
        rec=dict(region_id=q['region_id'],stage=q['stage'],days_complete=q['days_complete'],
                 months_complete=q.get('months_complete'),last_update=q['updated_utc'])
        if len(records)>1:
            hours=(dt.datetime.fromisoformat(records[-1]['time'])-dt.datetime.fromisoformat(records[0]['time'])).total_seconds()/3600
            rate=(records[-1]['months_complete']-records[0]['months_complete'])/hours
            rec.update(recent_months_per_hour=rate,remaining_weather_hours=(120-q['months_complete'])/rate if rate>0 else None)
        estimates.append(rec)
    pd.DataFrame(estimates).to_csv(out/'remaining_cube_estimates.csv',index=False)
    command=['ssh','-F','/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=15','ybai@volhynia.bgc-jena.mpg.de',
             'squeue -h -u ybai -o "%i|%j|%T|%M|%l|%C|%m|%R"']
    jobs=subprocess.run(command,capture_output=True,text=True,timeout=35,check=True).stdout
    jobs=[line for line in jobs.splitlines() if '|b1s-production|' in line]
    (out/'production_queue_snapshot.txt').write_text('\n'.join(jobs)+'\n')

    qa=pd.read_csv(root/'report_parallel_v1/qa_checks.csv');ids=sorted(qa.region_id.unique())
    profiles=pd.concat([pd.read_csv(root/'regions'/rid/'profile.csv') for rid in ids],ignore_index=True)
    checks=[]
    for variable in ['cEco','c_fire_efflux','evapotranspiration','TWS']:
        d=profiles[profiles.variable.eq(variable)];neg=d[d.negative.gt(0)]
        checks.append(dict(variable=variable,regions_with_negative=neg.region_id.nunique(),
                           minimum=float(d['min'].min()),negative_samples_both_scenarios=int(neg.negative.sum())))
    profiles[profiles.negative.gt(0)&profiles.variable.isin(['cEco','c_fire_efflux','evapotranspiration','TWS'])].to_csv(out/'negative_value_profiles.csv',index=False)
    pd.DataFrame(checks).to_csv(out/'negative_value_summary.csv',index=False)

    rows=[]
    for path in prepared['regions']:
        cols=['group_type','year','component','scenario','mean']
        d=pd.read_csv(Path(path)/'annual.csv',usecols=cols)
        d=d[d.group_type.eq('overall')&d.year.between(2015,2023)&d.component.isin(['nee','gpp','eco_respiration','fire_carbon_emission'])]
        a=d.pivot(index='year',columns=['component','scenario'],values='mean')
        assert len(a)==9 and np.isfinite(a.to_numpy()).all()
        net=a['nee','VIIRS_Fire']-a['nee','No_Fire']+a['fire_carbon_emission','VIIRS_Fire']-a['fire_carbon_emission','No_Fire']
        for year,value in net.items():
            rows.append(dict(region_id=Path(path).name,year=int(year),net_atmospheric_contribution=float(value),
                direct_fire=float(a.loc[year,('fire_carbon_emission','VIIRS_Fire')]),
                delta_nee=float(a.loc[year,('nee','VIIRS_Fire')]-a.loc[year,('nee','No_Fire')]),
                delta_gpp=float(a.loc[year,('gpp','VIIRS_Fire')]-a.loc[year,('gpp','No_Fire')]),
                delta_reco=float(a.loc[year,('eco_respiration','VIIRS_Fire')]-a.loc[year,('eco_respiration','No_Fire')])))
    contributions=pd.DataFrame(rows)
    contributions.to_csv(out/'annual_fire_contributions_247_regions.csv',index=False)
    case=contributions[contributions.region_id.eq(PORTUGAL)]
    selected=case[case.year.eq(2018)].iloc[0].to_dict()
    selected['cumulative_contribution_2015_2023']=float(case.net_atmospheric_contribution.sum())
    code=SOURCE_ROOT/PORTUGAL/f'output_WISP_{PORTUGAL}-full_No_Fire/code/model_functions.jl'
    code_text=code.read_text()
    assert 'evaporation = min(PET_evaporation, k_evaporation * (soilW[1] + ΔsoilW[1]))' in code_text
    raw=raw_check_2018(root)
    ev=dict(as_of_utc=now.isoformat(),snapshot=prepared['fingerprint'],completed_cubes=len(complete),remaining_cubes=len(estimates),
        live_production_jobs=len(jobs),live_production_running=sum('|RUNNING|' in x for x in jobs),
        estimate_method='Extrapolate recent six-hour month checkpoints per remaining cube; add final validation allowance; variable I/O means not a deadline.',
        negative_summary=checks,negative_annual_contribution_screen=dict(threshold_gC_m2=-.01,
            region_years=int(contributions.net_atmospheric_contribution.lt(-.01).sum()),
            regions=int(contributions[contributions.net_atmospheric_contribution.lt(-.01)].region_id.nunique())),
        portugal_2018=selected,raw_check=raw,
        evaporation_code=dict(path=str(code),sha256=file_hash(code),mechanism_status='plausible propagation path, not a traced first-negative timestep'),
        qa_source_sha256=file_hash(root/'report_parallel_v1/qa_checks.csv'))
    (out/'evidence.json').write_text(json.dumps(ev,indent=2)+'\n')
    return ev


def raw_check_2018(root):
    import xarray as xr
    root=Path(root)
    with np.load(root/'regions'/PORTUGAL/'spatial.npz') as z:
        area=z['owned_area_m2'].astype(float);mask=z['run_valid_mask'].astype(bool)&(area>0)
        weight=np.where(mask,area,0);lat=z['lat'];lon=z['lon']
    d=annual(root,PORTUGAL);rows=[]
    for scenario,variable,component in [('No_Fire','nee','nee'),('VIIRS_Fire','nee','nee'),
                                       ('VIIRS_Fire','c_fire_efflux','fire_carbon_emission')]:
        path=SOURCE_ROOT/PORTUGAL/f'output_WISP_{PORTUGAL}-full_{scenario}'/'data'/(variable+'.nc')
        with xr.open_dataset(path) as ds:
            np.testing.assert_allclose(ds.lat.values,lat,rtol=0,atol=1e-10)
            np.testing.assert_allclose(ds.lon.values,lon,rtol=0,atol=1e-10)
            idx=np.flatnonzero(ds.time.dt.year.values==2018)
            assert len(idx)==365 and np.all(np.diff(idx)==1)
            da=ds[variable];step=da.encoding['chunksizes'][da.dims.index('lon')];total=0.
            for start in range(0,len(lon),step):
                end=min(start+step,len(lon));block=da.isel(lon=slice(start,end),time=slice(idx[0],idx[-1]+1))
                extra=[x for x in block.dims if x not in ['lat','lon']]
                values=block.transpose('lat','lon',*extra).values.astype(float);selected=mask[:,start:end]
                assert np.isfinite(values[selected]).all()
                sums=values.sum(axis=tuple(range(2,values.ndim)))
                total+=np.sum(sums[selected]*weight[:,start:end][selected])
            actual=total/weight.sum();expected=float(series(d,variable,component,scenario).loc[2018])
            np.testing.assert_allclose(actual,expected,rtol=1e-6,atol=1e-5)
            rows.append(dict(scenario=scenario,variable=variable,raw=actual,summary=expected,
                             difference=abs(actual-expected),source=str(path),source_mtime_ns=path.stat().st_mtime_ns))
    return dict(status='pass',year=2018,days=365,checks=rows,
                net_contribution=rows[1]['raw']-rows[0]['raw']+rows[2]['raw'])


def build(root):
    root=Path(root);out=output(root);e=json.loads((out/'evidence.json').read_text())
    docs=Path(__file__).resolve().parents[1]/'docs';source=destination(root)/'email_three_findings_v1'
    template=(docs/'PROGRESS_EMAIL_20261105_EN.md').read_text()
    assert e['completed_cubes']==384 and e['remaining_cubes']==16, 'Refresh the draft status paragraph'
    assert e['live_production_running']==16
    text=template
    names=['01_Setif_productivity_vs_carbon_stock.png','02_Portugal_sink_source_with_combustion.png',
           '03_Nile_Delta_carbon_water_diagnostic.png']
    for name in names:shutil.copyfile(source/name,out/name)
    shutil.copyfile(source/'THREE_RESEARCH_FINDINGS_EN.pdf',out/'THREE_RESEARCH_FINDINGS_EN.pdf')
    (out/'PROGRESS_EMAIL_EN.md').write_text(text)
    import markdown
    body=markdown.markdown(text,extensions=['tables'])
    # Explicit email layout: simple fluid table; no web fonts, scripts or remote assets.
    body=body.replace('<p>','<p style="margin:0 0 14px;line-height:1.55;">').replace('<table>',
        '<table cellpadding="7" cellspacing="0" style="border-collapse:collapse;border:1px solid #d7dfe4;font-size:14px;margin:12px 0 18px;">')
    body=body.replace('<th>','<th style="text-align:left;background:#edf3f6;border:1px solid #d7dfe4;">').replace('<td>','<td style="border:1px solid #d7dfe4;">')
    frame='<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SINDBAD–WISP progress update</title></head><body style="margin:0;background:white;color:#24333d;font-family:Arial,sans-serif;font-size:15px;"><table role="presentation" width="100%"><tr><td align="center"><table role="presentation" width="760" style="max-width:100%;"><tr><td style="padding:24px;">'+body+'</td></tr></table></td></tr></table></body></html>'
    inline=frame;cid=frame
    for i,name in enumerate(names,1):
        data=base64.b64encode((out/name).read_bytes()).decode()
        inline=inline.replace('src="'+name+'"','src="data:image/png;base64,'+data+'" style="width:100%;height:auto;display:block;margin:12px 0;"')
        cid=cid.replace('src="'+name+'"','src="cid:figure'+str(i)+'" style="width:100%;height:auto;display:block;margin:12px 0;"')
    assert inline.count('src="data:image/png;base64,')==3 and cid.count('src="cid:figure')==3
    (out/'PROGRESS_EMAIL_EN.html').write_text(inline)
    import re
    plain=re.sub(r'!\[([^]]*)\]\([^)]+\)',r'[Attached figure: \1]',text).replace('**','')
    (out/'PROGRESS_EMAIL_EN.txt').write_text(plain)
    msg=EmailMessage(policy=SMTP);msg['Subject']='SINDBAD–WISP update: cube production and preliminary findings'
    msg['X-Unsent']='1';msg.set_content(plain);msg.add_alternative(cid,subtype='html')
    related=msg.get_payload()[1]
    for i,name in enumerate(names,1):related.add_related((out/name).read_bytes(),maintype='image',subtype='png',cid=f'<figure{i}>',filename=name,disposition='inline')
    msg.add_attachment((out/'THREE_RESEARCH_FINDINGS_EN.pdf').read_bytes(),maintype='application',subtype='pdf',filename='THREE_RESEARCH_FINDINGS_EN.pdf')
    (out/'PROGRESS_EMAIL_DRAFT.eml').write_bytes(msg.as_bytes())
    # A draft file only: no recipients, send API or SMTP connection.
    from email import policy
    from email.parser import BytesParser
    parsed=BytesParser(policy=policy.default).parsebytes((out/'PROGRESS_EMAIL_DRAFT.eml').read_bytes())
    assert parsed['X-Unsent']=='1' and parsed['To'] is None
    assert len([part for part in parsed.walk() if part.get_content_maintype()=='image'])==3
    members=['PROGRESS_EMAIL_DRAFT.eml','PROGRESS_EMAIL_EN.html','PROGRESS_EMAIL_EN.md','PROGRESS_EMAIL_EN.txt',
             'THREE_RESEARCH_FINDINGS_EN.pdf',*names,'evidence.json','remaining_cube_estimates.csv','negative_value_summary.csv']
    with zipfile.ZipFile(out/'PROGRESS_EMAIL_PACKAGE.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        for name in members:z.write(out/name,arcname=name)
    with zipfile.ZipFile(out/'PROGRESS_EMAIL_PACKAGE.zip') as z:assert z.testzip() is None
    record=dict(status='pass',sent=False,images=3,as_of_utc=e['as_of_utc'],
        code_sha256=file_hash(Path(__file__)),artifacts={name:file_hash(out/name) for name in members})
    (out/'delivery_validation.json').write_text(json.dumps(record,indent=2)+'\n')
    return dict(status='pass',sent=False,output=str(out),zip_bytes=(out/'PROGRESS_EMAIL_PACKAGE.zip').stat().st_size)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('stage',choices=['audit','build'])
    args=p.parse_args();print(json.dumps({'audit':audit,'build':build}[args.stage](args.root)))
