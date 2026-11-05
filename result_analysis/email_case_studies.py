"""Three compact research figures and an unsent supervisor-email draft.

Use the existing 293-region snapshot and 247-region primary QA gate. This is
an exploratory selection of case studies, not a new representative sample.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import zipfile

import numpy as np
import pandas as pd

from .findings_report import snapshot, destination, COLORS, SCENARIOS
from .io import file_hash
from . import report as old
from .parallel_report import merge_pdfs

SETIF = 'B1_DZA_Setif_near-Setif_36.4595N_005.3514E_T0765'
PORTUGAL = 'B1_PRT_Viseu_near-Vila-Real_41.0270N_008.0541W_T1494'
EGYPT = ['B1_EGY_Kafr-el-Sheikh_near-Kafr-el-Sheikh_31.0811N_030.8649E_T0125',
         'B1_EGY_Beheira_near-Damanhur_31.0811N_030.4324E_T0124']
SOURCE_ROOT = Path('/Net/Groups/BGI/tscratch/skoirala/WISP_V1_202610/entity')


def output(root):
    folder = destination(root) / 'email_three_findings_v1'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def annual(root, rid):
    d = pd.read_csv(Path(root) / 'regions' / rid / 'annual.csv')
    d = d[d.group_type.eq('overall') & d.year.between(2015, 2023)].copy()
    assert d.complete_period.all()
    assert not d.duplicated(['variable', 'component', 'scenario', 'year']).any()
    return d


def series(d, variable, component, scenario):
    a = d[d.variable.eq(variable) & d.component.eq(component) & d.scenario.eq(scenario)].set_index('year')['mean'].sort_index()
    assert list(a.index) == list(range(2015, 2024))
    assert np.isfinite(a).all()
    return a


def save(fig, folder, name):
    import matplotlib.pyplot as plt
    for suffix in ['png', 'pdf']:
        fig.savefig(folder / (name + '.' + suffix), dpi=220, facecolor='white')
    plt.close(fig)


def format_time(ax, years=(2015, 2017, 2019, 2021, 2023)):
    ax.set_xticks(years)
    ax.grid(axis='y', alpha=.18)
    ax.set_axisbelow(True)


def raw_portugal_check(root):
    """Recompute 2016 integrals directly from three original NetCDF variables."""
    import xarray as xr
    root = Path(root); folder = output(root)
    with np.load(root / 'regions' / PORTUGAL / 'spatial.npz') as z:
        area = z['owned_area_m2'].astype(float)
        mask = z['run_valid_mask'].astype(bool) & (area > 0)
        weight = np.where(mask, area, 0)
        lat, lon = z['lat'], z['lon']
    d = annual(root, PORTUGAL); records = []
    for scenario, variable, component in [('No_Fire','nee','nee'),
            ('VIIRS_Fire','nee','nee'), ('VIIRS_Fire','c_fire_efflux','fire_carbon_emission')]:
        path = SOURCE_ROOT / PORTUGAL / f'output_WISP_{PORTUGAL}-full_{scenario}' / 'data' / (variable+'.nc')
        with xr.open_dataset(path) as ds:
            np.testing.assert_allclose(ds.lat.values, lat, rtol=0, atol=1e-10)
            np.testing.assert_allclose(ds.lon.values, lon, rtol=0, atol=1e-10)
            idx = np.flatnonzero(ds.time.dt.year.values == 2016)
            assert len(idx) == 366 and np.all(np.diff(idx) == 1)
            da = ds[variable]; step = da.encoding['chunksizes'][da.dims.index('lon')]
            total = 0.
            for start in range(0, len(lon), step):
                end = min(start+step, len(lon))
                block = da.isel(lon=slice(start,end), time=slice(idx[0],idx[-1]+1))
                extra = [name for name in block.dims if name not in ['lat','lon']]
                values = block.transpose('lat','lon',*extra).values.astype(float)
                selected = mask[:,start:end]
                assert np.isfinite(values[selected]).all()
                sums = values.sum(axis=tuple(range(2,values.ndim)))
                total += np.sum(sums[selected] * weight[:,start:end][selected])
            actual = total / weight.sum()
            expected = float(series(d, variable, component, scenario).loc[2016])
            np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-5)
            records.append(dict(scenario=scenario,variable=variable,year=2016,n_days=366,
                source=str(path),source_bytes=path.stat().st_size,source_mtime_ns=path.stat().st_mtime_ns,
                raw_integral_gC_m2=actual,table_integral_gC_m2=expected,absolute_difference=abs(actual-expected)))
    state = dict(status='pass',snapshot=snapshot(root)['fingerprint'],checks=records,
        net_fire_scenario_gC_m2=records[1]['raw_integral_gC_m2']+records[2]['raw_integral_gC_m2'],
        scope='All owned runnable pixels in the Portugal case, all 366 days in 2016; NEE plus direct fire carbon, not gas-speciated CO2.')
    (folder/'portugal_2016_raw_check.json').write_text(json.dumps(state,indent=2)+'\n')
    return state


def figures(root):
    root=Path(root); folder=output(root); p=snapshot(root)
    assert all(str(root/'regions'/rid) in p['regions'] for rid in [SETIF,PORTUGAL,*EGYPT])
    raw=json.loads((folder/'portugal_2016_raw_check.json').read_text())
    assert raw['status']=='pass' and raw['snapshot']==p['fingerprint']
    plt=old._set_style(); plt.rcParams.update({'font.size':11,'axes.titlesize':12,'axes.labelsize':11})
    evidence={}; names=[]; source_paths=[]

    # 1. Productivity versus stock memory, with final stocks separated from means.
    d=annual(root,SETIF); d.to_csv(folder/'figure1_annual_values.csv',index=False)
    ep=pd.read_csv(destination(root)/'case_selection.csv').set_index('region_id').loc[SETIF]
    gpp=[series(d,'gpp','gpp',s) for s in SCENARIOS]
    frac=100*(gpp[1].mean()/gpp[0].mean()-1)
    fig,axes=plt.subplots(1,3,figsize=(14,5.7),gridspec_kw={'width_ratios':[1.15,1.15,.8]})
    for ax,(v,c,title,units) in zip(axes[:2],[('gpp','gpp','A  Annual photosynthetic uptake','g C m⁻² yr⁻¹'),
            ('cEco','cVeg','B  Annual mean vegetation carbon','g C m⁻²')]):
        for s,color,marker,ls in zip(SCENARIOS,COLORS,['o','s'],['-','--']):
            a=series(d,v,c,s);ax.plot(a.index,a.values,color=color,marker=marker,ms=4,lw=1.8,ls=ls,label=s.replace('_',' '))
        ax.set_title(title,loc='left');ax.set_ylabel(units);format_time(ax)
    axes[0].set_ylim(0,1500);axes[0].legend(frameon=False,fontsize=10,loc='lower left')
    axes[0].text(.03,.96,f'2015–2023 mean difference: {frac:.2f}%',transform=axes[0].transAxes,va='top',fontsize=10)
    vals=[ep['final__cVeg__'+s] for s in SCENARIOS]
    axes[2].bar([0,1],vals,color=COLORS,width=.6)
    axes[2].set_xticks([0,1],['No fire','VIIRS fire']);axes[2].set_ylabel('g C m⁻²');axes[2].set_ylim(0,880)
    axes[2].set_title('C  31 December 2023',loc='left');axes[2].grid(axis='y',alpha=.18);axes[2].set_axisbelow(True)
    for x,y in enumerate(vals):axes[2].text(x,y+12,f'{y:.1f}',ha='center',fontsize=11)
    axes[2].text(.5,.95,f"{ep.final_cVeg_relative_pct:.1f}% cVeg",transform=axes[2].transAxes,ha='center',va='top',weight='bold',color=COLORS[1])
    fig.suptitle('Similar GPP can coexist with a persistent vegetation-carbon deficit',x=.065,ha='left',fontsize=17,weight='semibold')
    fig.text(.065,.90,'Case: Sétif, Algeria · T0765 · 36.4595°N, 5.3514°E',fontsize=11)
    fig.text(.065,.04,'Same weather, initialization and static cover; paired model scenarios. Owned natural-vegetation support: 797 km².\nSelected strong-response case, not a typical effect. Annual mean stocks (B) and final-day stocks (C) are different summaries.',fontsize=9)
    fig.subplots_adjust(left=.065,right=.985,top=.78,bottom=.23,wspace=.35)
    name='01_Setif_productivity_vs_carbon_stock';save(fig,folder,name);names.append(name)
    evidence['figure1']=dict(region=SETIF,mean_annual_gpp_no_fire=float(gpp[0].mean()),mean_annual_gpp_viirs=float(gpp[1].mean()),
        gpp_change_pct=frac,final_cveg_no_fire=vals[0],final_cveg_viirs=vals[1],final_cveg_change_pct=float(ep.final_cVeg_relative_pct))

    # 2. A negative NEE need not imply a sink after combustion is included.
    d=annual(root,PORTUGAL); nf=series(d,'nee','nee','No_Fire');ff=series(d,'nee','nee','VIIRS_Fire')
    combustion=series(d,'c_fire_efflux','fire_carbon_emission','VIIRS_Fire');net=ff+combustion
    tab=pd.DataFrame({'No_Fire_NEE':nf,'VIIRS_NEE':ff,'direct_fire':combustion,'VIIRS_NEE_plus_fire':net})
    tab.to_csv(folder/'figure2_carbon_exchange.csv')
    fig,axes=plt.subplots(1,2,figsize=(13,5.8),gridspec_kw={'width_ratios':[1.45,1]})
    styles=[('No fire: NEE',nf,COLORS[0],'o','-'),('VIIRS fire: NEE',ff,COLORS[1],'s','--'),
            ('VIIRS fire: NEE + combustion',net,'#384F5A','D','-')]
    for label,arr,color,marker,ls in styles:axes[0].plot(arr.index,arr.values,color=color,marker=marker,ms=4,lw=1.8,ls=ls,label=label)
    axes[0].axhline(0,color='#555E64',lw=1);format_time(axes[0]);axes[0].set_ylabel('Annual carbon exchange (g C m⁻² yr⁻¹)')
    axes[0].set_title('A  Negative = uptake; positive = release',loc='left')
    axes[0].legend(frameon=False,fontsize=9,loc='lower left');axes[0].set_ylim(-105,21)
    axes[0].annotate(f'2016: {net.loc[2016]:+.1f}',xy=(2016,net.loc[2016]),xytext=(2017.1,14),
                     arrowprops=dict(arrowstyle='-',color='#384F5A'),fontsize=11)
    v=[ff.loc[2016],combustion.loc[2016],net.loc[2016]]
    axes[1].bar(range(3),v,color=[COLORS[0],COLORS[1],'#384F5A'],width=.6)
    axes[1].set_xticks(range(3),['NEE','Direct\ncombustion','Total']);axes[1].axhline(0,color='#555E64',lw=1)
    axes[1].set_title('B  2016 VIIRS-fire carbon budget',loc='left');axes[1].set_ylabel('g C m⁻² yr⁻¹')
    axes[1].set_ylim(-42,46);axes[1].grid(axis='y',alpha=.18);axes[1].set_axisbelow(True)
    for x,y in enumerate(v):axes[1].annotate(f'{y:+.1f}',(x,y),xytext=(0,7 if y>=0 else -17),textcoords='offset points',ha='center',fontsize=12)
    fig.suptitle('Including combustion changes the annual sink/source diagnosis',x=.075,ha='left',fontsize=17,weight='semibold')
    fig.text(.075,.90,'Case: near Vila Real, Portugal · T1494 · 41.0270°N, 8.0541°W',fontsize=11)
    fig.text(.075,.035,'Owned natural-vegetation support: 1,444 km². The 2016 totals were recomputed from the original daily NetCDF files.\nNEE excludes direct fire; mortality/transfers are not added again. Carbon mass, not separately resolved CO₂. Model finding, not an observed source.',fontsize=9)
    fig.subplots_adjust(left=.075,right=.98,top=.78,bottom=.25,wspace=.32)
    name='02_Portugal_sink_source_with_combustion';save(fig,folder,name);names.append(name)
    evidence['figure2']=dict(region=PORTUGAL,year=2016,no_fire_nee=float(nf.loc[2016]),viirs_nee=float(ff.loc[2016]),
        direct_combustion=float(combustion.loc[2016]),viirs_net=float(net.loc[2016]),raw_check='portugal_2016_raw_check.json')

    # 3. Same coarse cells/years per variable, two references, both scenarios.
    source=destination(root)/'references/annual_native_pairs.parquet';source_paths.append(source)
    native=pd.read_parquet(source);native=native[native.region_id.isin(EGYPT)]
    fig,axes=plt.subplots(2,2,figsize=(13.2,8.3));pairs=[];series_rows=[];recs=[]
    for row,(variable,modis,units) in enumerate([('gpp','MOD17A2HGF.061','g C m⁻² yr⁻¹'),
            ('evapotranspiration','MOD16A2GF.061','mm yr⁻¹')]):
        d=native[native.variable.eq(variable)].copy();d['lat_key']=d.reference_lat.round(3);d['lon_key']=d.reference_lon.round(3)
        a=d[d['product'].eq('FLUXCOM-X_X-BASE_v1.0')];b=d[d['product'].eq(modis)]
        key=['region_id','scenario','year','lat_key','lon_key']
        common=a.merge(b,on=key,suffixes=('_fluxcom','_modis'),validate='one_to_one');common['variable']=variable
        assert len(common)>0
        assert (common.reference_lat_fluxcom-common.reference_lat_modis).abs().max()<2e-5
        assert (common.reference_lon_fluxcom-common.reference_lon_modis).abs().max()<2e-5
        assert (common.model_fluxcom-common.model_modis).abs().max()<.05
        pairs.append(common)
        for col,rid in enumerate(EGYPT):
            ax=axes[row,col];q=common[common.region_id.eq(rid)]
            for scenario,column,color,marker,ls,label in [
                ('No_Fire','model_fluxcom',COLORS[0],'o','-','SINDBAD: No fire'),
                ('VIIRS_Fire','model_fluxcom',COLORS[1],'s','--','SINDBAD: VIIRS fire'),
                ('VIIRS_Fire','reference_modis','#8A813F','^','-', 'MOD17' if row==0 else 'MOD16'),
                ('VIIRS_Fire','reference_fluxcom','#B4678C','D','-','FLUXCOM-X')]:
                s=q[q.scenario.eq(scenario)].groupby('year')[column].mean()
                assert list(s.index)==list(range(2015,2022))
                ax.plot(s.index,s.values,color=color,marker=marker,ms=4,lw=1.7,ls=ls,label=label)
                series_rows.extend(dict(region_id=rid,variable=variable,series=label,year=int(y),mean=float(x),units=units) for y,x in s.items())
            short='Kafr el Sheikh · T0125' if col==0 else 'Beheira · T0124'
            ax.set_title(f'{chr(65+2*row+col)}  {short}',loc='left');ax.set_ylabel(('GPP' if row==0 else 'ET')+' ('+units+')')
            ax.set_ylim(0,2100 if row==0 else 1100);format_time(ax,years=[2015,2017,2019,2021])
            if col==0:ax.legend(frameon=False,fontsize=8.5,loc='center left',
                                bbox_to_anchor=(.01,.36 if row==0 else .30))
            qq=q[q.scenario.eq('VIIRS_Fire')]
            recs.append(dict(region_id=rid,variable=variable,n_cell_years=len(qq),n_cells=len(qq[['lat_key','lon_key']].drop_duplicates()),
                model=float(qq.model_fluxcom.mean()),fluxcom=float(qq.reference_fluxcom.mean()),modis=float(qq.reference_modis.mean()),
                max_model_difference_between_product_supports=float((qq.model_fluxcom-qq.model_modis).abs().max())))
    pd.concat(pairs,ignore_index=True).to_parquet(folder/'figure3_common_reference_pairs.parquet',index=False)
    pd.DataFrame(series_rows).to_csv(folder/'figure3_series.csv',index=False)
    pd.DataFrame(recs).to_csv(folder/'figure3_metrics.csv',index=False)
    fig.suptitle('Low GPP and ET persist even without fire: a baseline-model diagnostic',x=.075,ha='left',fontsize=16,weight='semibold')
    fig.text(.075,.91,'Two Nile Delta cubes · common reference cells and complete years (2015–2021)',fontsize=11)
    fig.text(.075,.025,'Same cells/years across products within each variable; GPP and ET supports differ. Equal weighting of paired coarse cells, not owned-area totals.\nSelected GPP-discrepancy cases; references are estimates. Check water supply, land-cover/support and parameters before assigning a cause.',fontsize=9)
    fig.subplots_adjust(left=.075,right=.98,top=.83,bottom=.13,wspace=.25,hspace=.40)
    name='03_Nile_Delta_carbon_water_diagnostic';save(fig,folder,name);names.append(name)
    evidence['figure3']=recs

    for rid in [SETIF,PORTUGAL]:source_paths.append(root/'regions'/rid/'annual.csv')
    source_paths += [destination(root)/'case_selection.csv',folder/'portugal_2016_raw_check.json',
                     destination(root)/'raw_case_endpoint_verification.json']
    pages=merge_pdfs([folder/(name+'.pdf') for name in names],folder/'THREE_RESEARCH_FINDINGS_EN.pdf')
    assert pages==3
    state=dict(status='complete',snapshot=p['fingerprint'],processed_regions=p['processed_regions'],primary_regions=p['primary_regions'],
        figures=names,evidence=evidence,source_checksums={str(x):file_hash(x) for x in source_paths},
        selection='Exploratory cases from the QA-passed snapshot: strong stock response; combustion-sensitive annual budget within an already selected case; multi-reference discrepancy. Not typical-site selection or independent hypothesis validation.')
    (folder/'evidence.json').write_text(json.dumps(state,indent=2)+'\n')
    return state


def package(root):
    """Package the three verified figures and an unsent draft, without networking."""
    from pypdf import PdfReader
    folder=output(root);state=json.loads((folder/'evidence.json').read_text())
    assert state['snapshot']==snapshot(root)['fingerprint']
    assert state['status']=='complete' and len(state['figures'])==3
    assert len(PdfReader(folder/'THREE_RESEARCH_FINDINGS_EN.pdf').pages)==3
    for name,digest in state['source_checksums'].items():
        if file_hash(Path(name))!=digest:
            raise ValueError('Source changed: '+name)
    docs=Path(__file__).resolve().parents[1]/'docs'
    for name in ['EMAIL_DRAFT_THREE_FINDINGS_EN.txt','EMAIL_CASE_STUDIES_EN.md']:
        shutil.copyfile(docs/name,folder/name)
    members=[*(name+'.png' for name in state['figures']),
             'THREE_RESEARCH_FINDINGS_EN.pdf','EMAIL_DRAFT_THREE_FINDINGS_EN.txt',
             'EMAIL_CASE_STUDIES_EN.md']
    archive=folder/'THREE_FINDINGS_EMAIL_PACK.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name in members:z.write(folder/name,arcname=name)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None and z.namelist()==members
    validation=dict(status='pass',snapshot=state['snapshot'],email_sent=False,
        processed_regions=state['processed_regions'],primary_regions=state['primary_regions'],
        pdf_pages=3,code_sha256=file_hash(Path(__file__)),
        checks=['Sources match evidence hashes','Three-page PDF opens',
                'Archive passes CRC check','Portugal raw annual integrals verified',
                'Setif endpoint check linked in evidence'],
        artifact_sha256={name:file_hash(folder/name) for name in [*members,'evidence.json',archive.name]})
    (folder/'delivery_validation.json').write_text(json.dumps(validation,indent=2)+'\n')
    return dict(status='pass',folder=str(folder),archive=str(archive),archive_bytes=archive.stat().st_size,
                email_sent=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('stage',choices=['raw-check','figures','package'])
    args=p.parse_args();state={'raw-check':raw_portugal_check,'figures':figures,'package':package}[args.stage](args.root)
    print(json.dumps(state))


if __name__=='__main__':main()
