import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import netCDF4 as nc
from result_analysis.registry import VARIABLES,SCENARIOS
from result_analysis.engine import process_region

class EngineTest(unittest.TestCase):
    def test_known_three_day_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);rid='B1_TEST';lat=np.array([40.,40.1]);lon=np.array([1.,1.1])
            for scenario in SCENARIOS:
                data=p/'source'/rid/('output_WISP_'+rid+'-full_'+scenario)/'data';data.mkdir(parents=True)
                for var,meta in VARIABLES.items():
                    with nc.Dataset(data/(var+'.nc'),'w') as ds:
                        for d,n in [('lon',2),('lat',2),('time',3)]:ds.createDimension(d,n)
                        ds.createVariable('lat','f8',('lat',))[:]=lat;ds.createVariable('lon','f8',('lon',))[:]=lon
                        t=ds.createVariable('time','f8',('time',));t.units='days since 2020-01-01';t[:]=[0,1,2]
                        dims=('lon','lat','time')
                        if len(meta['components'])>1:ld='d_tws' if var=='TWS' else 'd_cEco';ds.createDimension(ld,len(meta['components']));dims=('lon','lat',ld,'time')
                        v=ds.createVariable(var,'f4',dims,zlib=True);v.units=meta['units'];v[:]=0
                        if var=='gpp':v[:]=2
                        if var in ('auto_respiration','hetero_respiration'):v[:]=1
                        if var=='eco_respiration':v[:]=2
                        if var=='cEco':v[:]=10
                        ds.experiment='test_'+scenario
            cube=p/'batch'/'cubes'/(rid+'_frozen_v1');cube.mkdir(parents=True)
            with nc.Dataset(cube/('daily_master_dev_v5_'+rid+'_frozen_v1.nc'),'w') as ds:
                for d,n in [('lon',2),('lat',2),('time',3)]:ds.createDimension(d,n)
                ds.createVariable('viirs_fire_binary','f4',('time','lat','lon'))[:]=0
            groups=dict(lat=lat,lon=lon,run_valid_mask=np.array([[1,1],[1,0]],bool),owned_area_m2=np.array([[1.,1],[1,0]]),run_area_m2=np.ones((2,2)),group_area_m2={'overall:all':np.array([[1.,1],[1,0]])})
            config=dict(source_root=str(p/'source'),batch_root=str(p/'batch'),output_root=str(p/'out'),date_start='2020-01-01',date_end='2020-01-03',budget_model_tolerance=.01,analysis_scope='synthetic')
            with patch('result_analysis.engine.get_groups',return_value=groups):
                result=process_region(config,rid)
                self.assertEqual(result['scientific_status'],'pass')
                again=process_region(config,rid)
                self.assertEqual(result['fingerprint'],again['fingerprint'])
            with nc.Dataset(p/'out'/'regions'/rid/'compact.nc') as ds:
                self.assertEqual(float(ds.variables['gpp_monthly_sum'][0,0,0,0]),6)
                self.assertTrue(np.ma.is_masked(ds.variables['gpp_modis_sum'][0,0,0,0]))

if __name__=='__main__':unittest.main()
