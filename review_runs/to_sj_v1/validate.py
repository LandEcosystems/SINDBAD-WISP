"""Reopen all outputs and require the complete native README artifact contract."""
import argparse
import csv
from pathlib import Path
import numpy as np
import netCDF4 as nc
from PIL import Image
from prepare import read, write, sha, SCENARIOS


def require_file(path):
    path=Path(path)
    assert path.is_file() and path.stat().st_size > 0, 'Missing or empty: '+str(path)
    return dict(bytes=path.stat().st_size,sha256=sha(path))


def png(path):
    item=require_file(path)
    with Image.open(path) as img:
        assert img.format=='PNG' and min(img.size)>100
        img.verify()
    return item


def canonical(v, sl):
    dims=list(v.dimensions)
    order=['time']+[d for d in dims if d not in ('time','lat','lon')]+['lat','lon']
    a=np.ma.filled(v[sl],np.nan).transpose([dims.index(d) for d in order])
    return a[:,None] if a.ndim==3 else a


def validate(root):
    m=read(root/'review_manifest.json'); rid=m['region_id']
    audit=root/'audit'/rid; entity=root/'entity'/rid; domain=rid+'-full'
    maps=np.load(m['maps_path']); mask=maps['run_valid_mask'].astype(bool)
    config=read(Path(m['repo'])/'setups/WISP/experiment_WISP_DD.json')
    expected_vars={k.split('.')[-1] for k in config['model_output']['variables']}
    assert len(expected_vars)==13
    reports={}; inventory={}; all_files={}
    for scenario in SCENARIOS:
        run=entity/('output_WISP_'+domain+'_'+scenario)
        inp=read(audit/(scenario+'_input.json'))
        assert inp['passed'] and inp['mask_exact_match'] and inp['run_cells']==int(mask.sum())
        assert inp['unknown_years_preserved_as_9999']==m['unknown_years']
        inv=read(audit/(scenario+'_inventory.json'));inventory[scenario]=inv
        assert {Path(n).stem for n in inv['data_files']}==expected_vars
        records={}
        for name in sorted(expected_vars):
            path=run/'data'/(name+'.nc')
            with nc.Dataset(path) as ds:
                np.testing.assert_allclose(ds['lat'][:],maps['lat'],rtol=0,atol=1e-10)
                np.testing.assert_allclose(ds['lon'][:],maps['lon'],rtol=0,atol=1e-10)
                dates=nc.num2date(ds['time'][:],ds['time'].units)
                assert len(dates)==3641 and str(dates[0])[:10]=='2014-01-12' and str(dates[-1])[:10]=='2023-12-31'
                assert all((b-a).total_seconds()==86400 for a,b in zip(dates,dates[1:]))
                v=ds[name]; assert v.dtype==np.dtype('float32')
                expected_units='gC/m2' if name=='cEco' else 'mm' if name=='TWS' else 'mm/day' if name in ('evapotranspiration','transpiration','runoff') else 'gC/m2/day'
                assert v.units==expected_units, (name,v.units)
                layers=[d for d in v.dimensions if d not in ('time','lat','lon')]
                expected_layers=10 if name=='TWS' else 8 if name=='cEco' or name.startswith('c_fire_') else 1
                assert (len(ds.dimensions[layers[0]]) if layers else 1)==expected_layers
                assert v.filters()['zlib'] and v.filters()['complevel']==4
                lo,hi=np.inf,-np.inf
                for c in range(0,128,4):
                    sl=[slice(None)]*v.ndim;sl[v.dimensions.index('lon')]=slice(c,c+4)
                    a=canonical(v,tuple(sl)); keep=mask[:,c:c+4]
                    assert np.isfinite(a[:,:,keep]).all(),name+' invalid on run cells'
                    assert not np.isfinite(a[:,:,~keep]).any(),name+' excluded cells have values'
                    if keep.any():lo=min(lo,float(a[:,:,keep].min()));hi=max(hi,float(a[:,:,keep].max()))
                if scenario=='No_Fire' and name=='c_fire_efflux':assert lo==hi==0
                records[name]=dict(min=lo,max=hi,units=v.units,**require_file(path))
            all_files[str(path.relative_to(root))]=records[name]
        for sub in ['data/restart.nc','settings/experiment_WISP_DD.json','settings/forcing_WISP_DD.json',
                    'settings/model_structure_WISP_DD.json','settings/parameters.json','settings/info.jld2',
                    'code/model_definitions.jl','code/model_functions.jl','code/model_structs.jl']:
            p=run/sub;all_files[str(p.relative_to(root))]=require_file(p)
        for name in inv['figure_files']:
            p=run/'figure'/name;all_files[str(p.relative_to(root))]=png(p)
        reports[scenario]=dict(passed=True,variables=records,figures=len(inv['figure_files']),run_cells=int(mask.sum()))
    assert inventory['No_Fire']['series']==inventory['VIIRS_Fire']['series']
    series=inventory['No_Fire']['series']; comp=entity/'comparison'
    for scenario in SCENARIOS:
        p=comp/('summary_'+scenario+'.jls');all_files[str(p.relative_to(root))]=require_file(p)
    for name in series:
        for kind in ['mean','timeseries']:
            p=comp/f'{name}_{kind}_comparison_{domain}.png'
            all_files[str(p.relative_to(root))]=png(p)
    metrics=comp/f'comparison_metrics_{domain}.csv'
    with metrics.open() as f:
        reader=csv.DictReader(f)
        assert reader.fieldnames==['variable','scale','reference','other','n','r','r2','bias','rmse']
        rows=list(reader)
    expected={(name,scale,'No_Fire','VIIRS_Fire') for name in series for scale in ['spatial','temporal']}
    assert len(rows)==len(expected) and {(r['variable'],r['scale'],r['reference'],r['other']) for r in rows}==expected
    for r in rows:
        assert int(r['n'])==(int(mask.sum()) if r['scale']=='spatial' else 3641)
        assert np.isfinite(float(r['bias'])) and np.isfinite(float(r['rmse'])) and float(r['rmse'])>=0
        # Pearson r is undefined for a constant field; README explicitly permits NaN.
        for k in ['r','r2']:assert np.isfinite(float(r[k])) or np.isnan(float(r[k]))
    all_files[str(metrics.relative_to(root))]=require_file(metrics)
    # Same initialization, irrespective of subsequent fire forcing.
    dirs=[entity/('output_WISP_'+domain+'_'+s)/'data' for s in SCENARIOS]
    with nc.Dataset(dirs[0]/'restart.nc') as a,nc.Dataset(dirs[1]/'restart.nc') as b:
        assert set(a.variables)==set(b.variables)
        for n in a.variables:
            x,y=np.ma.filled(a[n][:],np.nan),np.ma.filled(b[n][:],np.nan)
            if np.issubdtype(np.asarray(x).dtype,np.number): assert np.array_equal(x,y,equal_nan=True),n
            else: assert np.array_equal(x,y),n
    for p,digest in m['identities'].items():assert sha(p)==digest,'Input/code changed during run: '+p
    note='VIIRS detections present but no modeled carbon response' if m['fire_positive_grid_days'] and reports['VIIRS_Fire']['variables']['c_fire_efflux']['max']==0 else 'Inspect the comparison figures for the modeled response.'
    result=dict(passed=True,region_id=rid,scenarios=reports,comparison_figures=2*len(series),
                comparison_metrics_rows=len(rows),paired_spinup_equal=True,
                identities_unchanged=True,fire_positive_grid_days=m['fire_positive_grid_days'],note=note,files=all_files)
    write(root/'review_validation.json',result)
    for scenario in SCENARIOS:
        p=root/'entity/done'/scenario/rid;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('README artifacts validated\n')
    p=root/'entity/done'/rid;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('Both scenarios and comparison validated\n')
    (root/'README_FOR_SUJAN.md').write_text(f'''# One-entity SINDBAD review\n\nAll requested native README artifacts passed validation.\n\n- Entity: `{rid}`.\n- Dates: 2014-01-12–2023-12-31 (3,641 days); 128 × 128 grid.\n- Run cells: {m['expected_run_cells']}; excluded cells: {m['excluded_cells']}; unknown years retained as 9999: {m['unknown_years']}.\n- Code commit: `{m['project_commit']}`. Original model configuration and plotting functions are unchanged.\n- `No_Fire`: VIIRS × 0. `VIIRS_Fire`: VIIRS × 0.99. WISP is not run.\n- Both scenarios share PFT parameters, spinup, static vegetation/tree backgrounds, and forcing.\n- Source depth is retained; effective model soil depth is capped by the configured 50 m column.\n\n## Files to inspect\n\n`entity/{rid}/` contains:\n\n- `output_WISP_{domain}_No_Fire/`: data, figure, settings, code.\n- `output_WISP_{domain}_VIIRS_Fire/`: data, figure, settings, code.\n- `comparison/`: {2*len(series)} comparison PNGs, {len(rows)} metric rows, and both serialized summaries.\n\nEach scenario has 13 daily output variables, restart.nc, and {reports['No_Fire']['figures']} figures. `audit/` records exact run masks, raw-source identities, effective overrides, and sentinel tests. `review_validation.json` reopens every NetCDF and PNG and checks the full expected inventory. Copied setup JSONs retain their defaults; `audit/*/*_overrides.json` and `settings/info.jld2` record effective overrides.\n\n## Interpretation\n\nVIIRS active fire scaled to 0.99 is an experimental burned-fraction proxy, not observed burned area. Emissions are carbon mass (gC m−2 day−1), not CO₂ mass; multiply carbon mass by 44/12 for CO₂. The original comparison CSV uses unweighted grid means, as documented upstream; it is not an area/selection-weighted Mediterranean emission estimate. Undefined correlation for constant fields may be NaN, as allowed by the README. {note}\n\nThe remaining Batch1 experiments are not automatically restarted by this review.\n''')
    print({k:v for k,v in result.items() if k not in ('files','scenarios')},flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();validate(a.root)
