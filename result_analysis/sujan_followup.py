"""Reference figures and first-recorded-fire stock analysis for the frozen 293 cohort.

Read-only sources. First recorded VIIRS forcing, not an independently delineated
wildfire event; previous saved daily stock is not the exact within-day fuel state.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import netCDF4 as nc
import numpy as np
import pandas as pd

from .grouping import get_groups, VEGETATION
from .registry import CARBON_POOLS
from .io import file_hash, atomic_json
from .report import _set_style, basic_metrics
from .findings_report import savefig
from .email_grouped_comparisons import distribution

NAME = 'report_findings_v3_all293/sujan_followup_v1'
GROUPS = ['forest', 'shrub', 'savanna', 'grass', 'wetland']
LABELS = ['Forest', 'Shrubland', 'Savanna', 'Grassland', 'Wetland']
COLORS = ['#207A64', '#B59B42', '#648FAD', '#B77544', '#8D6AA0']
SCENARIOS = ['No_Fire', 'VIIRS_Fire']
SERIES_COLORS = ['#276A84', '#CE713B', '#6F8065']


def settings(root):
    root = Path(root)
    cfg = json.loads((root/'config.json').read_text())
    inv = json.loads((root/'inventory.json').read_text())
    return cfg, sorted(x['region_id'] for x in inv['regions'])


def destination(root):
    out = Path(root)/NAME
    out.mkdir(parents=True, exist_ok=True)
    return out


def extract_region(root, rid):
    """Read only native longitude chunks containing eligible first-fire pixels."""
    started = time.time(); root = Path(root); cfg, _ = settings(root)
    out = destination(root)/'prefire_regions'; out.mkdir(exist_ok=True)
    done = out/(rid+'.json')
    if done.exists():
        old = json.loads(done.read_text())
        if old['code_sha256'] != file_hash(Path(__file__)):
            raise ValueError('Extraction code changed: use a new output version')
        if (out/(rid+'.parquet')).is_file():
            return old
    with np.load(root/'regions'/rid/'spatial.npz') as z:
        lat, lon = z['lat'], z['lon']
        first = z['first_fire_day'].astype(int)
        area = z['owned_area_m2']
        run = z['run_valid_mask'].astype(bool)
        counts = z['fire_day_count']
    valid = run & (area>0) & (first>0)
    rows, cols = np.where(valid); day = first[valid]
    dates = pd.date_range(cfg['date_start'], cfg['date_end'])
    d = pd.DataFrame(dict(region_id=rid, row=rows, col=cols, latitude=lat[rows], longitude=lon[cols],
                          first_fire_day=day, first_fire_date=dates[day].strftime('%Y-%m-%d'),
                          first_fire_year=dates[day].year, owned_area_m2=area[valid],
                          viirs_positive_days=counts[valid]))
    g = get_groups(rid, cfg['qa_root'], cfg['source_geometry'], cfg['pyrome_path'])
    np.testing.assert_allclose(g['owned_area_m2'], area, atol=.001, rtol=1e-9)
    d['model_pft'] = g['model_pft'][valid]
    for name in GROUPS:
        d['area_'+name+'_m2'] = g['group_area_m2']['vegetation:'+name][valid]
    np.testing.assert_allclose(d[['area_'+n+'_m2' for n in GROUPS]].sum(axis=1),d.owned_area_m2,rtol=1e-10,atol=.001)
    original = np.stack([g['group_area_m2']['igbp:'+str(k)][valid] for k in range(1,12)],axis=1)
    for k in range(1,12): d['area_igbp_'+str(k)+'_m2']=original[:,k-1]
    d['dominant_original_igbp'] = original.argmax(axis=1)+1
    identities = []
    for variable, prefix, offset in [('cEco','prefire_',-1),('c_fire_efflux','fire_day_',0)]:
        path = Path(cfg['source_root'])/rid/f'output_WISP_{rid}-full_VIIRS_Fire/data/{variable}.nc'
        stat=path.stat(); values=np.full((len(d),8),np.nan)
        with nc.Dataset(path) as ds:
            v=ds[variable]
            assert v.dimensions == ('lon','lat','d_cEco','time')
            np.testing.assert_allclose(ds['lat'][:],lat,rtol=0,atol=1e-10)
            np.testing.assert_allclose(ds['lon'][:],lon,rtol=0,atol=1e-10)
            t=ds['time']; dt=pd.DatetimeIndex([str(x)[:19] for x in nc.num2date(t[:],t.units,getattr(t,'calendar','standard'))])
            assert dt.equals(dates)
            step=int(v.chunking()[0]);v.set_auto_mask(False)
            for chunk in sorted(set(cols//step)):
                lo=int(chunk)*step; hi=min(lo+step,len(lon)); choose=(cols>=lo)&(cols<hi)
                b=np.asarray(v[lo:hi,:,:,:])
                values[choose]=b[cols[choose]-lo,rows[choose],:,day[choose]+offset]
            if len(values): assert np.isfinite(values).all() and (values>=0).all()
        assert path.stat().st_mtime_ns==stat.st_mtime_ns
        identities.append(dict(path=str(path),bytes=stat.st_size,mtime_ns=stat.st_mtime_ns))
        for k,pool in enumerate(CARBON_POOLS):d[prefix+pool+'_gC_m2']=values[:,k]
        for label,ix in [('cVeg',slice(0,4)),('cLitter',slice(4,6)),('cSoil',slice(6,8)),('cEcosystem',slice(0,8))]:
            d[prefix+label+'_gC_m2']=values[:,ix].sum(axis=1)
    # First calendar year paired response: includes subsequent detections that year.
    with nc.Dataset(root/'regions'/rid/'compact.nc') as ds:
        assert list(ds['scenario'][:])==SCENARIOS
        years=np.asarray(ds['year'][:]); yi=np.searchsorted(years,d.first_fire_year)
        for variable,component in [('gpp','gpp'),('nee','nee'),('eco_respiration','eco_respiration'),
                                   ('evapotranspiration','evapotranspiration'),('c_fire_efflux','fire_carbon_emission')]:
            a=np.asarray(ds[variable+'__'+component+'_annual'][:])
            for si,s in enumerate(SCENARIOS):d['first_year_'+component+'_'+s]=a[si,yi,rows,cols]
        d['first_year_complete']=d.first_fire_year>=2015
    d.to_parquet(out/(rid+'.parquet'),index=False)
    state=dict(region_id=rid,n_pixels=len(d),excluded_first_day=int((run&(area>0)&(first==0)).sum()),
               sources=identities,code_sha256=file_hash(Path(__file__)),elapsed_seconds=time.time()-started,
               stock_time='Previous saved day before first VIIRS-positive forcing; not exact within-day pre-combustion stock',
               event_definition='First recorded forcing day in simulated period; not first-ever wildfire')
    atomic_json(done,state)
    print(json.dumps(dict(region=rid,n_pixels=len(d),seconds=round(state['elapsed_seconds'],2))),flush=True)
    return state


def references(root):
    root=Path(root);out=destination(root); _,ids=settings(root)
    cols=['region_id','scenario','product','variable','year','reference_cell_id','reference_lat','reference_lon',
          'model','reference','units','coverage','accepted_90','covered_area_m2']
    frames=[];sources=[]
    for k,rid in enumerate(ids):
        p=root/'regions'/rid/'references/annual_reference_pairs.csv.gz'
        d=pd.read_csv(p,usecols=cols,dtype={'reference_cell_id':str})
        d=d[d.variable.isin(['gpp','nee','evapotranspiration']) & d.year.between(2015,2021)&
            d.accepted_90.eq(1)&np.isfinite(d.model)&np.isfinite(d.reference)].copy()
        frames.append(d);sources.append(dict(path=str(p),sha256=file_hash(p)))
    allpairs=pd.concat(frames,ignore_index=True)
    key=['product','variable','year','reference_cell_id']
    # Choose ownership using coverage only, independent of scenario and model error.
    owners=allpairs[allpairs.scenario.eq('No_Fire')].sort_values(['coverage','region_id'],ascending=[False,True],kind='stable').drop_duplicates(key)
    d=allpairs.merge(owners[key+['region_id']],on=key+['region_id'],validate='many_to_one')
    assert not d.duplicated(key+['scenario']).any()
    idx=key+['region_id','reference_lat','reference_lon','units']
    pair=d.pivot(index=idx,columns='scenario',values='model')
    refs=d.pivot(index=idx,columns='scenario',values='reference')
    areas=d.pivot(index=idx,columns='scenario',values='covered_area_m2')
    np.testing.assert_allclose(refs.No_Fire,refs.VIIRS_Fire,rtol=0,atol=0)
    np.testing.assert_allclose(areas.No_Fire,areas.VIIRS_Fire,rtol=1e-9)
    assert np.isfinite(pair[SCENARIOS]).all().all()
    pair['reference']=refs.No_Fire;pair['area_m2']=areas.No_Fire
    pair=pair.reset_index()
    # Match common GPP spatial cells/years between the two products.
    pair['lat_key']=pair.reference_lat.round(3);pair['lon_key']=pair.reference_lon.round(3)
    g=pair[pair.variable.eq('gpp')]
    common=g.groupby(['year','lat_key','lon_key']).product.nunique()
    common=common[common==2].reset_index()[['year','lat_key','lon_key']]
    g=g.merge(common,on=['year','lat_key','lon_key'],validate='many_to_one')
    pair=pd.concat([g,pair[~pair.variable.eq('gpp')]],ignore_index=True)
    pair.to_csv(out/'reference_annual_pairs_293.csv.gz',index=False,compression='gzip')
    summaries=[];metrics=[];yearly=[]
    for (product,var),q in pair.groupby(['product','variable']):
        for s in SCENARIOS:
            metrics.append(dict(product=product,variable=var,scenario=s,n_regions=q.region_id.nunique(),
                                years='2015-2021',**basic_metrics(q.reference,q[s])))
        for (rid,year),v in q.groupby(['region_id','year']):
            rec=dict(product=product,variable=var,region_id=rid,year=year,n_reference_cells=len(v),area_m2=v.area_m2.sum())
            for name in SCENARIOS+['reference']:rec[name]=np.average(v[name],weights=v.area_m2)
            yearly.append(rec)
    yearly=pd.DataFrame(yearly)
    for keys,q in yearly.groupby(['product','variable','region_id']):
        if set(q.year)!=set(range(2015,2022)):continue
        summaries.append(dict(zip(['product','variable','region_id'],keys),**{s:q[s].mean() for s in SCENARIOS+['reference']},n_years=7))
    summary=pd.DataFrame(summaries)
    summary.to_csv(out/'reference_region_means_293.csv',index=False)
    yearly.to_csv(out/'reference_region_annual_293.csv',index=False)
    pd.DataFrame(metrics).to_csv(out/'reference_metrics_293.csv',index=False)
    plt=_set_style()
    layout=[('gpp','MOD17A2HGF.061','GPP · MODIS','g C m⁻² yr⁻¹'),
            ('gpp','FLUXCOM-X_X-BASE_v1.0','GPP · FLUXCOM-X','g C m⁻² yr⁻¹'),
            ('nee','FLUXCOM-X_X-BASE_v1.0','NEE · FLUXCOM-X','g C m⁻² yr⁻¹'),
            ('evapotranspiration','MOD16A2GF.061','ET · MODIS','mm yr⁻¹'),
            ('evapotranspiration','FLUXCOM-X_X-BASE_v1.0','ET · FLUXCOM-X','mm yr⁻¹')]
    fig,axes=plt.subplots(2,5,figsize=(19,9))
    plotcounts=[]
    for j,(var,product,title,unit) in enumerate(layout):
        v=summary[summary.variable.eq(var)&summary['product'].eq(product)]
        assert len(v)>0,(product,pair['product'].unique())
        for x,(name,color) in enumerate(zip(SCENARIOS+['reference'],SERIES_COLORS)):
            distribution(axes[0,j],v[name],x,color,width=.7)
        axes[0,j].set_xticks([0,1,2],['No fire','VIIRS\nfire','Reference'],fontsize=9)
        axes[0,j].set_title(title+f'\n{len(v)} cubes, matched regional support',loc='left',fontsize=11)
        axes[0,j].set_ylabel(unit);axes[0,j].grid(axis='y',alpha=.18)
        q=pair[pair.variable.eq(var)&pair['product'].eq(product)]
        x=q.reference.to_numpy();y=q.VIIRS_Fire.to_numpy();ax=axes[1,j]
        ax.hexbin(x,y,gridsize=45,mincnt=1,bins='log',cmap='cividis',rasterized=True)
        lim=[min(x.min(),y.min()),max(x.max(),y.max())];ax.plot(lim,lim,color='#66747A',ls='--',lw=.8)
        m=basic_metrics(x,y)
        ax.text(.03,.97,f'n={len(q):,} cell-years\nRMSE={m["rmse"]:.1f}\nr={m["r"]:.2f}',transform=ax.transAxes,va='top',fontsize=9)
        ax.set_xlabel('Reference ('+unit+')');ax.set_ylabel('VIIRS-fire SINDBAD ('+unit+')')
        plotcounts.append(dict(product=product,variable=var,distribution_regions=len(v),scatter_regions=q.region_id.nunique(),cell_years=len(q)))
    fig.suptitle('Reference products alongside the two SINDBAD scenarios',x=.055,y=.97,ha='left',fontsize=20,weight='semibold')
    fig.text(.055,.91,'2015–2021 complete years · frozen 293-region cohort · reference-grid comparisons',fontsize=12)
    fig.text(.055,.02,'Top: each point is a cube’s seven-year mean annual total on matched reference support (distribution, not a confidence interval).\n'
             'Bottom: one unique reference cell-year; density colour indicates point count. Model aggregated to 0.05° reference support, coverage ≥90%; overlap duplicates removed.\n'
             'GPP products share coarse cells/years; masks can differ. ET products use their own valid supports. Negative NEE = uptake; direct fire is excluded from NEE.\n'
             'MODIS supplies GPP and ET here, not NEE. ET is water flux, not LE in W m⁻². References are estimates, not ground truth; completion subset is not a Mediterranean total.',fontsize=9)
    fig.subplots_adjust(left=.055,right=.985,top=.84,bottom=.20,wspace=.5,hspace=.5)
    savefig(fig,out/'01_reference_comparisons')
    atomic_json(out/'references_validation.json',dict(status='pass',regions_screened=len(ids),plots=plotcounts,sources=sources))
    return plotcounts


def merge_events(root):
    root=Path(root);out=destination(root);_,ids=settings(root)
    states=[json.loads((out/'prefire_regions'/(rid+'.json')).read_text()) for rid in ids]
    events=pd.concat([pd.read_parquet(out/'prefire_regions'/(rid+'.parquet')) for rid in ids],ignore_index=True)
    assert not events.duplicated(['region_id','row','col']).any()
    assert (events.first_fire_day>0).all()
    assert np.isfinite(events.select_dtypes(include='number')).all().all()
    events.to_csv(out/'prefire_pixel_pairs_293.csv.gz',index=False,compression='gzip')
    metrics=['prefire_cVeg_gC_m2','prefire_cLitter_gC_m2','prefire_cSoil_gC_m2','prefire_cEcosystem_gC_m2',
             'fire_day_cVeg_gC_m2','fire_day_cLitter_gC_m2','fire_day_cSoil_gC_m2','fire_day_cEcosystem_gC_m2']
    rows=[]
    for rid,q in events.groupby('region_id'):
        groups=[('overall','all',q.owned_area_m2)]
        groups += [('vegetation',g,q['area_'+g+'_m2']) for g in GROUPS]
        groups += [('igbp',str(g),q['area_igbp_'+str(g)+'_m2']) for g in range(1,12)]
        groups += [('model_pft',str(int(g)),q.owned_area_m2*(q.model_pft==g)) for g in sorted(q.model_pft.unique())]
        for family,g,w in groups:
            if w.sum()<=0:continue
            rec=dict(region_id=rid,group_type=family,group=g,n_pixels=int((w>0).sum()),affected_area_m2=w.sum())
            rec.update({m:np.average(q[m],weights=w) for m in metrics})
            rows.append(rec)
    summary=pd.DataFrame(rows);summary.to_csv(out/'prefire_stock_fire_by_region_group.csv',index=False)
    plt=_set_style();fig,axes=plt.subplots(1,3,figsize=(16,6.8))
    allcols=[('prefire_cVeg_gC_m2','fire_day_cVeg_gC_m2','Vegetation carbon','Combusted vegetation carbon'),
             ('prefire_cLitter_gC_m2','fire_day_cLitter_gC_m2','Litter carbon','Combusted litter carbon'),
             ('prefire_cEcosystem_gC_m2','fire_day_cEcosystem_gC_m2','Total ecosystem carbon','Total direct combustion')]
    for ax,(x,y,xlabel,ylabel) in zip(axes,allcols):
        for g,label,color in zip(GROUPS,LABELS,COLORS):
            q=summary[summary.group_type.eq('vegetation')&summary.group.eq(g)]
            ax.scatter(q[x],q[y],s=20,alpha=.65,c=color,label=f'{label} (n={len(q)})',edgecolors='none')
        ax.set_xscale('symlog',linthresh=1);ax.set_yscale('symlog',linthresh=1)
        ax.set_xlabel(xlabel+' before first fire (g C m⁻²)');ax.set_ylabel(ylabel+' on first day (g C m⁻²)')
        ax.set_title(xlabel,loc='left',fontsize=13);ax.grid(alpha=.15)
    axes[0].legend(frameon=False,fontsize=9,loc='upper left')
    fig.suptitle('Carbon present before fire and carbon released: differences among plant forms',x=.065,y=.97,ha='left',fontsize=18,weight='semibold')
    fig.text(.065,.895,f'{len(events):,} first-recorded-fire pixels across {events.region_id.nunique()} cubes · point = cube × vegetation class',fontsize=11)
    fig.text(.065,.025,'Stock = saved end-of-day state immediately before each pixel’s first VIIRS-positive forcing day; emission = that day’s matching pool efflux.\n'
             'Means use only owned VIIRS-affected class area; these are not measured burned-area values. Pixels are not independent wildfire events.\n'
             'Axes are logarithmic beyond 1 and linear near zero; zeros retained. Vegetation and litter emissions are paired with their respective source pools.\n'
             'Model fuel, combustion and mortality settings create part of this relationship; it is not independent evidence of causality or observed combustion efficiency.\n'
             '2014–2023; pixels burning on the first saved day have no prior stock and are excluded. End-of-day stocks differ from within-day pre-combustion stocks.',fontsize=9)
    fig.subplots_adjust(left=.065,right=.98,top=.81,bottom=.28,wspace=.32)
    savefig(fig,out/'02_prefire_stock_by_vegetation')
    # Model PFT is a separate parameter diagnostic, never substituted for original classes.
    pft=summary[summary.group_type.eq('model_pft')]
    codes=sorted(pft.group.unique(),key=int);fig,axes=plt.subplots(3,5,figsize=(16,10),sharex=True,sharey=True)
    for ax,g in zip(axes.flat,codes):
        q=pft[pft.group.eq(g)];ax.scatter(q.prefire_cVeg_gC_m2,q.fire_day_cVeg_gC_m2,color='#276A84',s=12,alpha=.65)
        ax.set_title(f'Model PFT {g} · {len(q)} cubes',loc='left',fontsize=10)
        ax.set_xscale('symlog',linthresh=1);ax.set_yscale('symlog',linthresh=1);ax.grid(alpha=.15)
    for ax in list(axes.flat)[len(codes):]:ax.set_visible(False)
    fig.suptitle('Model PFT diagnostic: pre-fire vegetation stock versus same-day combustion',x=.07,y=.97,ha='left',fontsize=17)
    fig.supxlabel('Previous-day cVeg (g C m⁻² of affected natural vegetation)',y=.08)
    fig.supylabel('Same-day vegetation combustion (g C m⁻²)',x=.02)
    fig.text(.07,.025,'Model PFTs include parameter-compatible recoding. Original plant-form classes are shown separately; one point per cube/PFT.\n'
             'First recorded VIIRS-positive day per pixel; a model diagnostic, not observed burned-area efficiency. Logarithmic axes beyond 1; zeros retained.',fontsize=9)
    fig.subplots_adjust(left=.08,right=.98,top=.90,bottom=.15,hspace=.4,wspace=.25)
    savefig(fig,out/'03_prefire_stock_by_model_pft')
    atomic_json(out/'prefire_validation.json',dict(status='pass',regions_screened=len(ids),regions_with_events=events.region_id.nunique(),
        first_fire_pixels=len(events),excluded_first_saved_day=sum(s['excluded_first_day'] for s in states),
        vegetation_area_partition='pass',unique_pixels='pass',nonnegative_stocks_and_direct_emissions='pass',
        sources=[s['sources'] for s in states]))
    return dict(pixels=len(events),regions=events.region_id.nunique())


def negative(root):
    root=Path(root);out=destination(root)
    src=root/'report_findings_v3_all293/fire_site_cases_v1/annual_fire_metrics_all293.csv'
    d=pd.read_csv(src);d['minus_delta_gpp']=-d.delta_gpp
    np.testing.assert_allclose(d.direct_fire+d.minus_delta_gpp+d.delta_eco_respiration,d.net_fire_contribution,atol=.001,rtol=0)
    assert d.direct_fire.min()>=0
    d['negative_net_below_minus_001']=d.net_fire_contribution<-.01
    d.to_csv(out/'annual_net_carbon_decomposition_293.csv',index=False)
    chosen=d.sort_values('net_fire_contribution').drop_duplicates('region_id').head(3)
    plt=_set_style();fig,axes=plt.subplots(1,3,figsize=(15,6))
    for ax,row in zip(axes,chosen.itertuples()):
        vals=[row.direct_fire,row.minus_delta_gpp,row.delta_eco_respiration,row.net_fire_contribution]
        ax.bar(range(4),vals,color=['#CE713B','#B59B42','#276A84','#344957'],width=.65)
        ax.axhline(0,color='#66747A',lw=.7);ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
        ax.set_xticks(range(4),['Direct\ncombustion','Reduced GPP\n(−ΔGPP)','Respiration\nchange','Net\ncontribution'],fontsize=9)
        for x,y in enumerate(vals):ax.annotate(f'{y:+.2f}',(x,y),xytext=(0,5 if y>=0 else -15),textcoords='offset points',ha='center',fontsize=10)
        ax.margins(y=.23);ax.set_ylabel('g C m⁻² yr⁻¹ (owned natural vegetation)')
        short=row.region_id.split('_');ax.set_title(f'{short[1]} · {short[-1]} · {row.year}',loc='left',fontsize=13)
    fig.suptitle('Why a negative net fire contribution is not negative combustion',x=.07,y=.97,ha='left',fontsize=18,weight='semibold')
    fig.text(.07,.88,'Net contribution = direct combustion + ΔReco − ΔGPP     (Δ = VIIRS fire − no fire)',fontsize=12)
    fig.text(.07,.025,f'Exploratory cases: most negative annual contribution from three distinct cubes. {d.negative_net_below_minus_001.sum()} of {len(d)} cube-years are below −0.01 g C m⁻² yr⁻¹.\n'
             'Reduced respiration can exceed combustion plus lost uptake in an individual year. This arithmetic does not establish the biological cause.\n'
             'Denominator here is all owned natural vegetation, including unburned cells. Annual negative differences do not imply negative direct emissions or a beneficial cumulative fire effect.',fontsize=9)
    fig.subplots_adjust(left=.07,right=.98,top=.78,bottom=.25,wspace=.4)
    savefig(fig,out/'04_negative_net_decomposition')
    atomic_json(out/'negative_validation.json',dict(status='pass',source_sha256=file_hash(src),n_cube_years=len(d),
        negative_direct=int((d.direct_fire<0).sum()),negative_net_below_minus_001=int(d.negative_net_below_minus_001.sum()),
        max_decomposition_residual=float((d.direct_fire+d.minus_delta_gpp+d.delta_eco_respiration-d.net_fire_contribution).abs().max())))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['extract','references','merge','negative']);p.add_argument('root',type=Path)
    p.add_argument('--index',type=int,default=0);p.add_argument('--workers',type=int,default=1);p.add_argument('--region')
    a=p.parse_args()
    if a.stage=='extract':
        _,ids=settings(a.root)
        for rid in ([a.region] if a.region else ids[a.index::a.workers]):extract_region(a.root,rid)
    else:print(json.dumps({'references':references,'merge':merge_events,'negative':negative}[a.stage](a.root),default=int))
