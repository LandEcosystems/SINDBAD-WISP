"""Native-chunk, read-only paired entity analysis. No model is executed here."""
import csv, gc, hashlib, json, os, time
from pathlib import Path
import netCDF4 as nc
import numpy as np
import pandas as pd
from .registry import VARIABLES, DERIVED, SCENARIOS
from .grouping import get_groups
from .numerics import time_groups, period_reduce, metrics, rounding_bound, budget_check
from .io import atomic_json, exclusive, canonical_hash, file_hash, source_identity

def _read(v, lo, hi):
    v.set_auto_mask(False)
    a=np.asarray(v[lo:hi],dtype=np.float32)
    for attr in ('_FillValue','missing_value'):
        if hasattr(v,attr): a[a==getattr(v,attr)]=np.nan
    # Normalize (lon,lat,[layer],time) to (lon,lat,layer,time).
    return a[:,:,None,:] if a.ndim==3 else a

def _check(checks,name,failures,n=None,**kwargs):
    checks.append(dict(check=name,status='fail' if failures else 'pass',failures=int(failures),n=n,**kwargs))

def _csv(path,rows,columns=None):
    pd.DataFrame(rows,columns=columns).to_csv(path,index=False)

def process_region(config, rid, snapshot_fingerprint=''):
    root=Path(config['output_root']);dest=root/'regions'/rid
    dest.mkdir(parents=True,exist_ok=True)
    with exclusive(dest/'analysis.lock'):
        return _process(config,rid,dest,snapshot_fingerprint)

def _process(config,rid,dest,snapshot_fingerprint):
    start=time.time(); paths=[]
    for s in SCENARIOS:
        run=Path(config['source_root'])/rid/('output_WISP_'+rid+'-full_'+s)
        paths.extend(run/'data'/(v+'.nc') for v in VARIABLES)
    identity=source_identity(paths)
    code_identity={name:file_hash(Path(__file__).with_name(name)) for name in ('engine.py','numerics.py','grouping.py','registry.py','io.py')}
    fp=canonical_hash(dict(config=config,sources=identity,code=code_identity))
    done=dest/'completion.json'
    if done.exists():
        old=json.loads(done.read_text())
        if old.get('fingerprint')==fp and all((dest/p).exists() for p in old['artifacts']):
            return old
        raise RuntimeError('Existing region has a different identity; use a new output version: '+rid)
    atomic_json(dest/'progress.json',dict(region_id=rid,status='starting',pid=os.getpid(),fingerprint=fp))
    groups=get_groups(rid,Path(config['batch_root'])/'qa')
    lat=np.asarray(groups['lat']);lon=np.asarray(groups['lon'])
    runmask=np.asarray(groups['run_valid_mask'],bool).T
    # Grouping arrays are lat,lon; scientific files store lon,lat.
    ga={k:np.asarray(a,dtype=float).T for k,a in groups['group_area_m2'].items() if np.sum(a)>0}
    if 'overall:all' not in ga:ga['overall:all']=np.asarray(groups['owned_area_m2']).T
    sample=paths[0]
    with nc.Dataset(sample) as ds:
        t=ds.variables['time']; dates=pd.DatetimeIndex([str(x)[:19] for x in nc.num2date(t[:],t.units,getattr(t,'calendar','standard'))])
    expected=pd.date_range(config['date_start'],config['date_end'])
    if not dates.equals(expected):raise ValueError('Unexpected or incomplete time axis: '+rid)
    nt=len(dates);nx,ny=len(lon),len(lat);tg=time_groups(dates);checks=[]
    spatial=dict(lat=lat,lon=lon,owned_area_m2=np.asarray(groups['owned_area_m2']),run_valid_mask=runmask.T)
    annual_rows=[];monthly_rows=[];profile=[];legacy={};qa_maps={}
    # Small enough to retain total carbon/fluxes for exact pixel-day identities.
    totals={};bounds={};scalar={}; all_digests={}
    cube_path=Path(config['batch_root'])/'cubes'/(rid+'_frozen_v1')/('daily_master_dev_v5_'+rid+'_frozen_v1.nc')
    with nc.Dataset(cube_path) as ds:
        fire=ds.variables['viirs_fire_binary'];dims=fire.dimensions
        firearr=np.asarray(fire[:]);firearr=np.transpose(firearr,[dims.index('lon'),dims.index('lat'),dims.index('time')])
        ever_fire=np.any(firearr>0,axis=-1)&runmask
        first=np.argmax(firearr>0,axis=-1);first[~ever_fire]=-1
        repeated=np.sum(firearr>0,axis=-1)>1
    spatial.update(first_fire_day=first.T,fire_day_count=np.sum(firearr>0,axis=-1).T)
    del firearr
    compact_tmp=dest/'compact.partial.nc'
    with nc.Dataset(compact_tmp,'w') as out:
        out.createDimension('scenario',2);out.createDimension('lat',ny);out.createDimension('lon',nx)
        out.createVariable('scenario',str,('scenario',))[:]=np.asarray(SCENARIOS,object)
        out.createVariable('lat','f8',('lat',))[:]=lat;out.createVariable('lon','f8',('lon',))[:]=lon
        out.createVariable('run_valid_mask','u1',('lat','lon'))[:]=runmask.T
        out.createVariable('owned_area_m2','f8',('lat','lon'))[:]=groups['owned_area_m2']
        forest=np.asarray(groups.get('run_group_area_m2',{}).get('vegetation:forest',np.zeros((ny,nx)))).T
        cell_area=np.asarray(groups['run_area_m2']).T
        frac=np.divide(forest,cell_area,out=np.zeros_like(forest),where=cell_area>0)
        out.createVariable('forest_fraction','f4',('lat','lon'))[:]=frac.T
        out.createVariable('cell_area_m2','f8',('lat','lon'))[:]=cell_area.T
        for kind in ('month','year','composite'):
            out.createDimension(kind,len(tg[kind]))
            if kind=='year':out.createVariable('year','i4',('year',))[:]=[int(x[0]) for x in tg[kind]]
            for suffix,idx in [('start',2),('end',3)]:
                v=out.createVariable(kind+'_'+suffix,'f8',(kind,));v.units='days since 1970-01-01';v.calendar='standard'
                v[:]=[(x[idx]-pd.Timestamp('1970-01-01')).days for x in tg[kind]]
        out.analysis_scope=config['analysis_scope'];out.source_fingerprint=fp
        out.area_basis='Native model per-m2; no vegetation-fraction rescaling; absolute regional observational totals gated pending denominator verification'
        def add_spatial_var(name,dimension,unit):
            v=out.createVariable(name,'f4',('scenario',dimension,'lat','lon'),zlib=True,complevel=2,fill_value=np.nan)
            v.units=unit;return v
        # Reference-ready summaries; no hourly/daily full output replication.
        refs={}
        for var in ('gpp','nee','evapotranspiration','transpiration'):
            refs[var]=add_spatial_var(var+'_monthly_sum','month',VARIABLES[var]['units'].replace(' day-1',''))
        refs['gpp_modis']=add_spatial_var('gpp_modis_sum','composite','g C m-2 per composite')
        refs['wood']=add_spatial_var('cVegWood_annual_mean','year','g C m-2')
        for variable,meta in VARIABLES.items():
            atomic_json(dest/'progress.json',dict(region_id=rid,status='reading',variable=variable,elapsed_seconds=time.time()-start))
            print(rid,variable,round(time.time()-start,1),'s',flush=True)
            pair_paths=[Path(config['source_root'])/rid/('output_WISP_'+rid+'-full_'+s)/'data'/(variable+'.nc') for s in SCENARIOS]
            derived={k:m for k,m in DERIVED.items() if m['source']==variable}
            labels=list(meta['components'])+list(derived)
            # Annual maps remain compact, enabling reference/group reanalysis without rereading ten years.
            avars={lab:add_spatial_var(variable+'__'+lab+'_annual','year',meta['units'].replace(' day-1','')) for lab in labels}
            buf={s:{lab:dict(mean=np.full((nx,ny),np.nan),daily_sum=np.zeros(nt),daily_n=np.zeros(nt),
                                      monthly=np.full((len(tg['month']),nx,ny),np.nan),annual=np.full((len(tg['year']),nx,ny),np.nan)) for lab in labels} for s in SCENARIOS}
            # Keep only the few full daily sums used by the carbon accounting checks.
            if variable in ('cEco','c_fire_efflux'):
                for s in SCENARIOS:totals[s,variable]=np.zeros((nx,ny,nt),np.float32);bounds[s,variable]=np.zeros((nx,ny,nt),np.float32)
            if variable=='cEco':
                for s in SCENARIOS:scalar[s,'cVeg']=np.empty((nx,ny,nt),np.float32)
            if variable in ('gpp','auto_respiration','hetero_respiration','eco_respiration','nee','evapotranspiration'):
                for s in SCENARIOS:scalar[s,variable]=np.empty((nx,ny,nt),np.float32)
            with nc.Dataset(pair_paths[0]) as d0,nc.Dataset(pair_paths[1]) as d1:
                datasets=[d0,d1];vs=[d.variables[variable] for d in datasets]
                for si,d in enumerate(datasets):
                    v=vs[si]
                    expected_dims=('lon','lat','time') if len(meta['components'])==1 else ('lon','lat','d_tws' if variable=='TWS' else 'd_cEco','time')
                    if v.dimensions != expected_dims:raise ValueError('Unexpected dimensions '+variable+str(v.dimensions))
                    if len(meta['components'])>1 and v.shape[2]!=len(meta['components']):raise ValueError('Unexpected layer count '+variable)
                    actual_unit=str(getattr(v,'units','')).replace(' ','').replace('m-2','/m2').replace('day-1','/day').replace('gC','gC')
                    expected_unit=meta['units'].replace(' ','').replace('m-2','/m2').replace('day-1','/day')
                    if actual_unit!=expected_unit:raise ValueError('Unit mismatch '+variable+': '+actual_unit+' != '+expected_unit)
                    for coord,ref in [('lat',lat),('lon',lon)]:
                        if not np.allclose(d.variables[coord][:],ref,rtol=0,atol=1e-9):raise ValueError('Coordinate mismatch '+variable)
                    tv=d.variables['time'];td=pd.DatetimeIndex([str(x)[:19] for x in nc.num2date(tv[:],tv.units,getattr(tv,'calendar','standard'))])
                    if not td.equals(dates):raise ValueError('Variable time mismatch '+variable)
                    if SCENARIOS[si] not in str(getattr(d,'experiment','')):raise ValueError('Scenario identity mismatch '+variable)
                chunk=vs[0].chunking();step=int(chunk[0]) if isinstance(chunk,list) else 8
                hashes=[hashlib.sha256(),hashlib.sha256()]
                counts={s:{lab:dict(n=0,nan=0,inf=0,negative=0,zero=0,min=np.inf,max=-np.inf,max_daily_jump=0.,constant_pixels=0,no_fire_difference=0) for lab in labels} for s in SCENARIOS}
                for lo in range(0,nx,step):
                    hi=min(lo+step,nx);arrs=[_read(v,lo,hi) for v in vs]
                    for h,a in zip(hashes,arrs):h.update(np.ascontiguousarray(a).tobytes())
                    for si,(s,a) in enumerate(zip(SCENARIOS,arrs)):
                        mask=runmask[lo:hi]; eligible=mask[...,None]
                        if (s,variable) in scalar:scalar[s,variable][lo:hi]=a[:,:,0,:]
                        if variable=='cEco':scalar[s,'cVeg'][lo:hi]=np.sum(a[:,:,:4,:],axis=2,dtype=np.float32)
                        if (s,variable) in totals:
                            totals[s,variable][lo:hi]=np.sum(a,axis=2,dtype=np.float32)
                            bounds[s,variable][lo:hi]=np.sum(rounding_bound(a),axis=2)
                        aa={lab:a[:,:,idx,:] for idx,lab in enumerate(meta['components'])}
                        aa.update({lab:np.sum(a[:,:,m['indices'],:],axis=2,dtype=np.float32) for lab,m in derived.items()})
                        for lab,x in aa.items():
                            rec=counts[s][lab];b=buf[s][lab];z=x[mask];finite=np.isfinite(z)
                            rec['n']+=z.size;rec['nan']+=int(np.isnan(z).sum());rec['inf']+=int(np.isinf(z).sum())
                            rec['negative']+=int(np.count_nonzero(z[finite]<0));rec['zero']+=int(np.count_nonzero(z[finite]==0))
                            if np.any(finite):rec['min']=min(rec['min'],float(z[finite].min()));rec['max']=max(rec['max'],float(z[finite].max()))
                            if z.size:
                                rec['max_daily_jump']=max(rec['max_daily_jump'],float(np.nanmax(np.abs(np.diff(z,axis=-1)))))
                                rec['constant_pixels']+=int(np.count_nonzero(np.all(np.isfinite(z),axis=-1)&(np.max(z,axis=-1)==np.min(z,axis=-1))))
                            b['mean'][lo:hi]=np.mean(x,axis=-1,dtype=np.float64)
                            if variable in ('cEco','TWS'):
                                for tag,ti in [('initial_saved',0),('final_saved',-1)]:
                                    skey=variable+'__'+lab+'__'+tag+'__'+s
                                    if skey not in spatial:spatial[skey]=np.full((ny,nx),np.nan,dtype='f4')
                                    spatial[skey][:,lo:hi]=np.where(mask,x[:,:,ti],np.nan).T
                            # Legacy check intentionally reproduces unmasked old CSV, separately from science statistics.
                            good=np.isfinite(x);b['daily_sum']+=np.sum(np.where(good,x,0),axis=(0,1),dtype=np.float64);b['daily_n']+=np.sum(good,axis=(0,1))
                            kind='flux' if meta['kind']=='sumflux' else 'stock'
                            for period in ('month','year'):
                                target=b['monthly' if period=='month' else 'annual']
                                for j,(_,inds,_,_) in enumerate(tg[period]):target[j,lo:hi]=period_reduce(x,inds,kind)
                            if lab==meta['components'][0] and variable in refs:
                                refs[variable][si,:,:,lo:hi]=b['monthly'][:,lo:hi].transpose(0,2,1)
                            if variable=='gpp':
                                for j,(_,inds,begin,end) in enumerate(tg['composite']):
                                    val=period_reduce(x,inds,'flux') if len(inds)==(end-begin).days else np.full(mask.shape,np.nan)
                                    refs['gpp_modis'][si,j,:,lo:hi]=val.T
                            if variable=='cEco' and lab=='wood':refs['wood'][si,:,:,lo:hi]=b['annual'][:,lo:hi].transpose(0,2,1)
                    # Compare ALL daily values in pixels without any positive VIIRS day.
                    dry=runmask[lo:hi]&~ever_fire[lo:hi]
                    diff=arrs[1]-arrs[0]
                    for li,lab in enumerate(meta['components']):
                        n=int(np.count_nonzero(np.isfinite(diff[:,:,li,:][dry])&(diff[:,:,li,:][dry]!=0)))
                        counts[SCENARIOS[1]][lab]['no_fire_difference']+=n
                    del arrs,aa,a,x,z,diff
                for si,s in enumerate(SCENARIOS):
                    all_digests[s+'/'+variable]=hashes[si].hexdigest()
                    for lab,b in buf[s].items():
                        rec=counts[s][lab];profile.append(dict(region_id=rid,scenario=s,variable=variable,component=lab,**rec))
                        _check(checks,s+'/'+variable+'/'+lab+'/finite',rec['nan']+rec['inf'],rec['n'])
                        if meta['nonnegative']:_check(checks,s+'/'+variable+'/'+lab+'/nonnegative',rec['negative'],rec['n'])
                        if s=='No_Fire' and variable.startswith('c_fire'):_check(checks,s+'/'+variable+'/'+lab+'/zero',rec['n']-rec['zero'],rec['n'])
                        if s=='VIIRS_Fire' and lab in meta['components']:_check(checks,variable+'/'+lab+'/no_fire_pixels_identical',rec['no_fire_difference'])
                        key=variable+'__'+lab
                        spatial[key+'__'+s]=np.where(runmask,b['mean'],np.nan).T.astype('f4')
                        legacy[s,key]=(b['mean'],np.divide(b['daily_sum'],b['daily_n'],out=np.full(nt,np.nan),where=b['daily_n']>0))
                        avars[lab][si,:,:,:]=np.where(runmask[None,:,:],b['annual'],np.nan).transpose(0,2,1)
                        for period,rows in [('month',monthly_rows),('year',annual_rows)]:
                            ar=b['monthly' if period=='month' else 'annual']
                            for j,(_,inds,begin,end) in enumerate(tg[period]):
                                vals=ar[j]; valid=np.isfinite(vals)&runmask
                                for group,area in ga.items():
                                    w=np.where(valid,area,0);den=float(w.sum())
                                    if den==0:continue
                                    gtype,gname=group.split(':',1)
                                    row=dict(region_id=rid,scenario=s,year=begin.year,variable=variable,component=lab,
                                        group_type=gtype,group=gname,mean=float(np.sum(np.where(valid,vals,0)*w)/den),
                                        valid_area_m2=den,n_days=len(inds),complete_period=len(inds)==(end-begin).days,
                                        aggregation='sum' if meta['kind']=='sumflux' else 'mean',units=meta['units'].replace(' day-1','') if meta['kind']=='sumflux' else meta['units'])
                                    if period=='month':row['month']=begin.month
                                    rows.append(row)
            del buf;gc.collect()
            out.sync()
        for s in SCENARIOS:
            # Check pointwise identities with propagated quantization envelopes.
            for name,left,right in [('nee_identity','nee',scalar[s,'eco_respiration']-scalar[s,'gpp']),('respiration_identity','eco_respiration',scalar[s,'auto_respiration']+scalar[s,'hetero_respiration'])]:
                x=scalar[s,left];r=x-right
                operands=['nee','eco_respiration','gpp'] if left=='nee' else ['eco_respiration','auto_respiration','hetero_respiration']
                bound=sum(rounding_bound(scalar[s,v]) for v in operands)+2e-6
                ok=runmask[...,None]&np.isfinite(r)
                _check(checks,s+'/'+name,int(np.count_nonzero(ok&(np.abs(r)>bound))),int(ok.sum()),maximum_abs=float(np.max(np.abs(r[ok]))))
            result=budget_check(totals[s,'cEco'],scalar[s,'nee'],totals[s,'c_fire_efflux'],bounds[s,'cEco'],rounding_bound(scalar[s,'nee']),bounds[s,'c_fire_efflux'],runmask,config['budget_model_tolerance'])
            _check(checks,s+'/carbon_budget',**result)
            residual=np.diff(totals[s,'cEco'],axis=-1)+scalar[s,'nee'][...,1:]+totals[s,'c_fire_efflux'][...,1:]
            qa_maps[s+'__carbon_budget_max_abs']=np.max(np.abs(residual),axis=-1).T
        # Compact event-aligned diagnostics from GPP; repeated positives are not separate independent fires.
        recovery=[]
        for offset in [-365,-180,-90,-30,0,30,90,180,365,730]:
            d=first+offset;ok=ever_fire&(d>=0)&(d<nt)
            ii,jj=np.where(ok);tt=d[ok]
            for state,sel in [('all',np.ones(len(ii),bool)),('repeated_positive_days',repeated[ii,jj]),('single_positive_day',~repeated[ii,jj])]:
                w=ga['overall:all'][ii,jj][sel];den=w.sum()
                for var in ('gpp','nee','evapotranspiration','cVeg'):
                    delta=scalar['VIIRS_Fire',var][ii,jj,tt]-scalar['No_Fire',var][ii,jj,tt]
                    recovery.append(dict(region_id=rid,variable=var,lag_days=offset,fire_class=state,n_pixels=int(sel.sum()),area_m2=float(den),mean_delta=float(np.sum(w*delta[sel])/den) if den else np.nan))
        _csv(dest/'recovery.csv',recovery)
    os.replace(compact_tmp,dest/'compact.nc')
    _csv(dest/'annual.csv',annual_rows);_csv(dest/'monthly.csv',monthly_rows);_csv(dest/'profile.csv',profile)
    np.savez_compressed(dest/'spatial.npz',**spatial);np.savez_compressed(dest/'qa_maps.npz',**qa_maps)
    # Recompute existing comparison CSV independently from decoded NetCDF arrays.
    oldcsv=Path(config['source_root'])/rid/'comparison'/('comparison_metrics_'+rid+'-full.csv')
    legacy_rows=[]
    if oldcsv.exists():
        old=pd.read_csv(oldcsv)
        for variable,meta in VARIABLES.items():
            prefix='pools' if meta['kind']=='meanstock' else ('diagnostics' if variable.startswith('c_fire') else 'fluxes')
            for li,lab in enumerate(meta['components']):
                key=variable+'__'+lab;tag=prefix+'_'+variable+('_'+str(li+1) if len(meta['components'])>1 else '')
                for idx,scale in enumerate(('spatial','temporal')):
                    m=metrics(legacy['No_Fire',key][idx],legacy['VIIRS_Fire',key][idx]);match=old[(old.variable==tag)&(old.scale==scale)]
                    row=dict(region_id=rid,variable=variable,component=lab,scale=scale,**m)
                    if len(match)==1:
                        for field in ('bias','rmse','r','r2'):row['legacy_'+field]=match.iloc[0][field];row['difference_'+field]=m[field]-match.iloc[0][field]
                    row['legacy_rows']=len(match);legacy_rows.append(row)
    _csv(dest/'legacy_csv_recheck.csv',legacy_rows)
    _check(checks,'source_files_unchanged',0 if source_identity(paths)==identity else 1)
    qa=dict(region_id=rid,checks=checks,status='fail' if any(c['status']=='fail' for c in checks) else 'pass',
            caveats=['Four-digit quantization propagated; first restart not used as forward initial state.',
                     'Reference agreement is a separate stage. LAI unavailable.',
                     'Large temporal jumps profiled but not automatically invalidated; fires cause real jumps.'],
            source_array_sha256=all_digests,source_metadata=identity,source_fingerprint=fp,
            owned_area_m2=float(ga['overall:all'].sum()),run_pixels=int(runmask.sum()),elapsed_seconds=time.time()-start)
    atomic_json(dest/'qa.json',qa)
    artifacts=['compact.nc','annual.csv','monthly.csv','profile.csv','spatial.npz','qa_maps.npz','legacy_csv_recheck.csv','recovery.csv','qa.json']
    # Completion means processing completed; scientific acceptance remains separate.
    result=dict(region_id=rid,fingerprint=fp,snapshot=snapshot_fingerprint,status='processed',scientific_status=qa['status'],
                elapsed_seconds=time.time()-start,artifacts=artifacts,checksums={p:file_hash(dest/p) for p in artifacts})
    atomic_json(done,result);atomic_json(dest/'progress.json',result)
    return result
