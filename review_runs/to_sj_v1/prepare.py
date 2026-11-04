"""Freeze one accepted entity and the validated runtime, without editing sources."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import shutil

import netCDF4 as nc
import numpy as np

BASE = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube')
PROD = BASE / 'shift_v3_60km'
RUNTIME_ROOT = BASE / 'sindbad_fire_nofire_batch1_v1'
DEFAULT_ROOT = RUNTIME_ROOT / 'outputs/to_SJ'
SCENARIOS = ('No_Fire', 'VIIRS_Fire')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + '\n')
    tmp.replace(path)


def verify_runtime():
    identity = read(RUNTIME_ROOT / 'runtime/identity.json')
    checks = dict(identity['source_file_sha256'])
    checks.update({str(RUNTIME_ROOT / k): v for k, v in identity['frozen_files'].items()})
    checks[str(RUNTIME_ROOT / 'runtime/sindbad_entity.so')] = identity['system_image_sha256']
    for p, digest in checks.items():
        if not Path(p).is_file() or sha(p) != digest:
            raise RuntimeError('Validated runtime changed: ' + p)
    return checks


def make_adapter(path, lat, lon, years, mask):
    with nc.Dataset(path, 'w') as ds:
        for name, x, unit in [('lat', lat, 'degrees_north'), ('lon', lon, 'degrees_east')]:
            ds.createDimension(name, len(x))
            v = ds.createVariable(name, 'f8', (name,)); v[:] = x; v.units = unit
        v = ds.createVariable('disturbance_year_model', 'f4', ('lat', 'lon'), fill_value=np.float32(np.nan))
        v[:] = np.where(mask, years, np.nan).astype('f4')
        v.units = '1'
        v.comment = '9999 remains an intentional unknown/no-disturbance sentinel. Only excluded pixels are NaN.'


def prepare(root, repo):
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'review_manifest.json'
    if target.exists():
        m = read(target)
        for p, digest in m['identities'].items():
            if sha(p) != digest:
                raise RuntimeError('Frozen review identity changed: ' + p)
        return m
    identities = verify_runtime()
    receipts = [read(p) for p in (PROD / 'reports/acceptance').glob('*.json')]
    a = max((a for a in receipts if a.get('passed') and not a.get('errors')), key=lambda a: a['checked_utc'])
    rid = a['region_id']; cube = Path(a['path'])
    assert sha(cube) == a['file_sha256']
    regions = read(PROD / 'sampling/named_production_regions.json')['regions']
    r = next(r for r in regions if r['region_id'] == rid)
    assert sha(a['masks_path']) == a['masks_sha256'] == r['masks_sha256']
    assert sha(a['owner_path']) == a['owner_sha256'] == r['owner_sha256']
    mask = np.load(a['masks_path'])['run_valid_mask'].astype(bool)
    assert mask.shape == (128, 128) and mask.any()
    inp = root / 'audit' / rid; inp.mkdir(parents=True, exist_ok=True)
    with nc.Dataset(cube) as ds:
        assert len(ds.variables) == 24 and len(ds.dimensions['time']) == 3641
        dates = nc.num2date(ds['time'][:], ds['time'].units)
        assert str(dates[0])[:10] == '2014-01-12' and str(dates[-1])[:10] == '2023-12-31'
        assert all((b-a).total_seconds() == 86400 for a,b in zip(dates, dates[1:]))
        lat, lon = np.asarray(ds['lat'][:]), np.asarray(ds['lon'][:])
        np.testing.assert_allclose(lat, r['lat'], rtol=0, atol=1e-10)
        np.testing.assert_allclose(lon, r['lon'], rtol=0, atol=1e-10)
        ds['disturbance_year'].set_auto_maskandscale(False)
        years = np.asarray(ds['disturbance_year'][:])
        assert np.all((years[mask] < 2014) | (years[mask] == 9999))
        depth = np.ma.filled(ds['soil_depth_SoilGrids'][:], np.nan)
        assert np.all(np.isfinite(depth[mask]) & (depth[mask] > 0))
        pft = np.asarray(ds['f_pft'][:])
        assert set(np.unique(pft[mask])).issubset(set(range(1,13)) | {15})
        fire_count = 0
        for i in range(0,3641,31):
            f = np.ma.filled(ds['viirs_fire_binary'][i:i+31], np.nan)
            assert np.isfinite(f[:,mask]).all()
            fire_count += int((f[:,mask] > 0).sum())
    adapter = inp / 'disturbance_year_model_adapter.nc'
    make_adapter(adapter, lat, lon, years, mask)
    # A small I/O test covers the sentinel and excluded-mask case even if this entity is all land.
    test = inp / 'adapter_test.nc'
    make_adapter(test, np.array([1.,2.]), np.array([3.,4.]),
                 np.array([[9999,2013],[1980,1850]]), np.array([[True,True],[False,True]]))
    with nc.Dataset(test) as ds:
        x = np.ma.filled(ds['disturbance_year_model'][:], np.nan)
        assert x[0,0] == 9999 and x[0,1] == 2013 and np.isnan(x[1,0]) and x[1,1] == 1850
    maps = inp / 'model_input_maps.npz'
    np.savez_compressed(maps, run_valid_mask=mask, lat=lat, lon=lon, disturbance_year=years,
                        source_soil_depth_m=depth, effective_soil_depth_m=np.minimum(depth,50))
    expected = inp / 'expected_cells.csv'
    with expected.open('w') as f:
        f.write('lat_index_1based,lon_index_1based,disturbance_year\n')
        for i,j in np.argwhere(mask): f.write(f'{i+1},{j+1},{int(years[i,j])}\n')
    shutil.copy2(PROD/'reports/acceptance'/(rid+'.json'), inp/'source_acceptance.json')
    docs = root/'documentation'; docs.mkdir(exist_ok=True)
    for rel in ['README.md','README_SINDBAD_WISP_OUTPUT.md','slurm_scripts/README_SLURM.md']:
        shutil.copy2(repo/rel, docs/Path(rel).name)
    for rel in ['setups/WISP/experiment_WISP_DD.json','setups/WISP/forcing_WISP_DD.json',
                'setups/WISP/model_structure_WISP_DD.json','plot_forward_WISP_DD.jl',
                'README.md','README_SINDBAD_WISP_OUTPUT.md','slurm_scripts/README_SLURM.md']:
        identities[str(repo/rel)] = sha(repo/rel)
    for p in (repo/'review_runs/to_sj_v1').iterdir():
        if p.is_file(): identities[str(p)] = sha(p)
    for p in [cube,adapter,maps,expected,Path(a['masks_path']),Path(a['owner_path'])]: identities[str(p)] = sha(p)
    m = dict(region_id=rid, cube=str(cube), file_sha256=a['file_sha256'], root=str(root), repo=str(repo),
             adapter=str(adapter), maps_path=str(maps), expected_cells=str(expected),
             expected_run_cells=int(mask.sum()), excluded_cells=int((~mask).sum()),
             unknown_years=int((years[mask]==9999).sum()), fire_positive_grid_days=fire_count,
             selected_by='Latest successful acceptance timestamp; fixed once; no selection on model response',
             acceptance_utc=a['checked_utc'], selected_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
             project_commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],universal_newlines=True).strip(),
             runtime_environment=str(RUNTIME_ROOT/'runtime/environment'),
             system_image=str(RUNTIME_ROOT/'runtime/sindbad_entity.so'),
             parameters=str(RUNTIME_ROOT/'runtime/parameters.json'),
             soil_depth=dict(source_min_m=float(depth[mask].min()), source_max_m=float(depth[mask].max()),
                             cells_over_50m=int((depth[mask]>50).sum()), effective_rule='min(raw,50m); source unchanged'),
             scenarios=dict(No_Fire=0.0,VIIRS_Fire=0.99), identities=identities)
    write(target,m)
    (root/'logs').mkdir(exist_ok=True)
    write(root/'audit/adapter_test.json',dict(passed=True,sentinel_9999_preserved=True,excluded_nan=True))
    print(json.dumps({k:m[k] for k in ['region_id','expected_run_cells','excluded_cells','unknown_years','fire_positive_grid_days','soil_depth']},indent=2))
    return m


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[2]); a=p.parse_args()
    prepare(a.root,a.repo)
