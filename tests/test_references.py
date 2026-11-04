"""Known-population tests for native-support reference comparisons."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np

from result_analysis.references import (
    aggregate_to_reference, annualize_reference_pairs, common_annual_gpp_pairs, cell_bounds, cci_pairs, compare_region,
    fluxcom_hourly_to_monthly, official_monthly_to_daily_rate, integrate_daily, modis_intervals,
    overlap_matrix, rectangle_area, weighted_metrics,
)


class ReferenceMathTests(unittest.TestCase):
    def test_common_annual_gpp_uses_exact_support_and_coverage_dedup(self):
        base=dict(scenario="No_Fire",variable="gpp",accepted_90=1,period_start="2015-01-01",period_end="2016-01-01",
                  reference_lat=35.025,reference_lon=10.025,coverage=.95,region_id="b",model=100,reference=110)
        rows=[dict(base,product="MOD17A2HGF.061"),dict(base,product="FLUXCOM-X_X-BASE_v1.0")]
        rows.append(dict(rows[0],region_id="a",coverage=.99,model=120))
        rows.append(dict(rows[0],reference_lon=10.075)) # no matching second product
        common=common_annual_gpp_pairs(rows)
        self.assertEqual(len(common),2)
        self.assertEqual(common[0]["region_id"],"a")
        self.assertEqual(common[0]["model_value"],120)
        self.assertEqual(common[1]["reference_product"],"FLUXCOM-X_X-BASE_v1.0")

    def test_complete_annual_flux_and_partial_year(self):
        base=dict(region_id="r",scenario="No_Fire",product="p",variable="gpp",reference_cell_id=1,
                  carbon_fraction="",coverage=1,covered_area_m2=2,reference_support_area_m2=2,
                  model_value=0,reference_value=0,accepted_90=1,accepted_80=1,units="gC m-2 month-1")
        records=[]
        from result_analysis.references import calendar_months
        for a,b in calendar_months(2016):
            d=int((b-a).astype(int))
            records.append(dict(base,period_start=str(a),period_end=str(b),days=d,model=d*2,reference=d))
        result=annualize_reference_pairs(records)[0]
        self.assertEqual(result["model"],732)
        self.assertEqual(result["reference"],366)
        self.assertEqual(result["accepted_90"],1)
        missing=annualize_reference_pairs(records[1:])[0]
        self.assertEqual(missing["accepted_90"],0)
        self.assertTrue(np.isnan(missing["model"]))

    def test_modis_last_interval_and_leap_year(self):
        self.assertEqual(len(modis_intervals(2019)), 46)
        self.assertEqual(int(np.diff(modis_intervals(2019)[-1])[0].astype(int)), 5)
        self.assertEqual(int(np.diff(modis_intervals(2020)[-1])[0].astype(int)), 6)

    def test_partial_year_is_not_integrated_as_complete(self):
        dates = np.arange(np.datetime64("2014-01-12"), np.datetime64("2014-02-01"))
        x = np.ones((len(dates), 2, 2))
        result = integrate_daily(x, dates, modis_intervals(2014)[:4])
        self.assertTrue(np.isnan(result[:2]).all())
        np.testing.assert_equal(result[2], 8)
        self.assertTrue(np.isnan(result[3]).all())

    def test_missing_day_value_strictly_propagates(self):
        dates = np.arange(np.datetime64("2020-02-01"), np.datetime64("2020-03-01"))
        x = np.ones((29, 2, 2)); x[4, 0, 0] = np.nan
        result = integrate_daily(x, dates, [(dates[0], np.datetime64("2020-03-01"))])
        self.assertTrue(np.isnan(result[0, 0, 0]))
        self.assertEqual(result[0, 1, 1], 29)

    def test_duplicate_date_fails(self):
        with self.assertRaises(ValueError):
            integrate_daily(np.ones((2,1,1)), ["2020-01-01", "2020-01-01"], [])

    def test_ellipsoid_areas_add(self):
        whole = rectangle_area(np.array([35, 36]), np.array([10, 11]))
        pieces = rectangle_area(np.array([[35,35.5],[35.5,36]])[:,None,:],
                                np.array([[10,10.5],[10.5,11]])[None,:,:])
        self.assertAlmostEqual(float(pieces.sum()/whole), 1, places=12)
        self.assertTrue(9.9e9 < whole < 1.1e10)

    def test_overlap_mean_and_mask(self):
        matrix, area = overlap_matrix([35.25,35.75], [10.25,10.75], [35.5], [10.5],
                                      reference_lat_bounds=[[35,36]], reference_lon_bounds=[[10,11]])
        x = np.full((1,2,2), 3.0)
        means, coverage, overlap = aggregate_to_reference(x,matrix,area,np.ones((2,2),bool))
        self.assertAlmostEqual(means[0,0], 3)
        self.assertAlmostEqual(coverage[0,0], 1)
        self.assertAlmostEqual(overlap[0,0]/area[0], 1)
        mask=np.array([[1,0],[1,0]],bool)
        means, coverage, _=aggregate_to_reference(x,matrix,area,mask)
        self.assertAlmostEqual(means[0,0], 3)
        self.assertAlmostEqual(coverage[0,0], .5)

    def test_descending_coordinates_preserve_data_alignment(self):
        kwargs=dict(reference_lat_bounds=[[35,36]], reference_lon_bounds=[[10,11]])
        a, area = overlap_matrix([35.25,35.75], [10.25,10.75], [35.5], [10.5], **kwargs)
        b, area2 = overlap_matrix([35.75,35.25], [10.75,10.25], [35.5], [10.5], **kwargs)
        x=np.array([[[1.,2.],[3.,4.]]])
        first=aggregate_to_reference(x,a,area,np.ones((2,2),bool))[0]
        second=aggregate_to_reference(x[:,::-1,::-1],b,area2,np.ones((2,2),bool))[0]
        np.testing.assert_allclose(first,second)

    def test_no_overlap_is_empty_not_renormalized(self):
        a,area=overlap_matrix([35.25,35.75],[10.25,10.75],[40.5],[10.5],
                             reference_lat_bounds=[[40,41]],reference_lon_bounds=[[10,11]])
        value,coverage,_=aggregate_to_reference(np.ones((1,2,2)),a,area,np.ones((2,2),bool))
        self.assertEqual(a.nnz,0);self.assertEqual(coverage[0,0],0)
        self.assertTrue(np.isnan(value[0,0]))

    def test_forest_fraction_limits_support_and_does_not_scale_stock(self):
        a,area=overlap_matrix([35.25,35.75],[10.25,10.75],[35.5],[10.5],
                             reference_lat_bounds=[[35,36]],reference_lon_bounds=[[10,11]])
        value,coverage,_=aggregate_to_reference(np.full((1,2,2),100.),a,area,np.ones((2,2),bool),np.full((2,2),.5))
        self.assertAlmostEqual(value[0,0],100.)
        self.assertAlmostEqual(coverage[0,0],.5)

    def test_fluxcom_carbon_and_water_units(self):
        x=np.ones((2,24,1,1))
        np.testing.assert_allclose(fluxcom_hourly_to_monthly(x,"GPP"),1.0368)
        np.testing.assert_allclose(fluxcom_hourly_to_monthly(x,"ET"),24)
        x[0,0,0,0]=np.nan
        self.assertTrue(np.isnan(fluxcom_hourly_to_monthly(x,"GPP")[0,0,0]))
        with self.assertRaises(ValueError):
            fluxcom_hourly_to_monthly(x[:,:23],"ET")
        a,multiplier=official_monthly_to_daily_rate(np.array([.1]),"ET","mm hr-1")
        np.testing.assert_allclose(a,[2.4]);self.assertEqual(multiplier,24)
        a,multiplier=official_monthly_to_daily_rate(np.array([3.]),"GPP","gC m-2 d-1")
        np.testing.assert_equal(a,[3.]);self.assertEqual(multiplier,1)

    def test_metrics_zero_variance_not_fabricated_correlation(self):
        result=weighted_metrics([2,2],[1,1],[1,3])
        self.assertEqual(result["bias"],1)
        self.assertIsNone(result["pearson_r"])
        self.assertIsNone(result["nse"])

    def test_r_squared_is_not_nse(self):
        result=weighted_metrics([2,4,6],[1,2,3])
        self.assertAlmostEqual(result["r_squared"],1)
        self.assertLess(result["nse"],0)


class ReferenceIntegrationTests(unittest.TestCase):
    def test_official_cache_requires_full_lossless_readback(self):
        import xarray as xr
        from result_analysis.references import validate_official_monthly_cache,calendar_months
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"cache.nc"
            values=np.ones((12,2,2),np.float32);values[0,0,0]=np.nan
            ds=xr.Dataset({"GPP":(("time","latitude","longitude"),values)},
                          coords={"time":[a for a,b in calendar_months(2017)],"latitude":[35.,36.],"longitude":[10.,11.]})
            ds.GPP.attrs["units"]="gC m-2 day-1";ds.to_netcdf(path)
            self.assertTrue(validate_official_monthly_cache(path,"GPP",2017,values)["full_array_decoded"])
            with self.assertRaises(ValueError):
                validate_official_monthly_cache(path,"GPP",2017,np.zeros_like(values))
            path.write_bytes(b"not a NetCDF file")
            with self.assertRaises(OSError):
                validate_official_monthly_cache(path,"GPP",2017)

    def test_fluxcom_monthly_first_partial_month_and_global_ids(self):
        import xarray as xr
        import json,hashlib
        from result_analysis.references import fluxcom_verified_monthly_pairs
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            source=xr.Dataset({"GPP":(("time","latitude","longitude"),np.ones((2,2,2)))},
                              coords={"time":np.array(["2014-01-01","2014-02-01"],dtype="datetime64[D]"),"latitude":[35.25,35.75],"longitude":[10.25,10.75]})
            source.GPP.attrs["units"]="gC m-2 day-1"
            source.attrs.update(source_grid_columns=7200,source_row_offset=1000,source_col_offset=2000)
            source.to_netcdf(p/"source.nc")
            manifest=dict(product="FLUXCOM-X X-BASE v1.0",complete_months_only=True,files=[dict(variable="GPP",path="source.nc",source_url="https://data.icos-cp.eu/objects/approved_object",sha256=hashlib.sha256((p/"source.nc").read_bytes()).hexdigest())])
            (p/"manifest.json").write_text(json.dumps(manifest))
            summary=xr.Dataset({"gpp_monthly_sum":(("scenario","month","lat","lon"),np.full((1,2,2,2),28.)),
                                "month_start":("month",np.array(["2014-01-01","2014-02-01"],dtype="datetime64[D]")),
                                "month_end":("month",np.array(["2014-02-01","2014-03-01"],dtype="datetime64[D]")),
                                "run_valid_mask":(("lat","lon"),np.ones((2,2),bool))},
                               coords={"scenario":["No_Fire"],"lat":[35.25,35.75],"lon":[10.25,10.75]})
            rows=list(fluxcom_verified_monthly_pairs(summary,"r",p/"manifest.json"))
            self.assertEqual(rows[0]["reference_cell_id"],1000*7200+2000)
            self.assertTrue(all(r["accepted_90"]==0 for r in rows if r["period_start"]=="2014-01-01"))
            feb=[r for r in rows if r["period_start"]=="2014-02-01"]
            self.assertTrue(all(r["accepted_90"]==1 and r["reference"]==28 for r in feb))

    def test_real_netcdf_adapter_no_double_modis_scale_and_resume(self):
        import xarray as xr
        from netCDF4 import Dataset,date2num
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            # Equal source/model grid makes coverage exact and test auditable.
            source=xr.Dataset({"Gpp":(("time","latitude","longitude"),np.full((46,2,2),.008))},
                              coords={"time":[a for a,b in modis_intervals(2015)],"latitude":[35.25,35.75],"longitude":[10.25,10.75]})
            source.Gpp.attrs.update(units="kg_C_/m^2²",scale_factor=1.)
            source.to_netcdf(p/"gpp_2015.nc")
            summary=xr.Dataset({
                "gpp_modis_sum":(("scenario","composite","lat","lon"),np.full((2,46,2,2),8.)),
                "run_valid_mask":(("lat","lon"),np.ones((2,2),np.uint8)),
                "forest_fraction":(("lat","lon"),np.ones((2,2))),
                "composite_start":("composite",[a for a,b in modis_intervals(2015)]),
                "composite_end":("composite",[b for a,b in modis_intervals(2015)]),
            },coords={"scenario":["No_Fire","VIIRS_Fire"],"composite":np.arange(46),"lat":[35.25,35.75],"lon":[10.25,10.75]})
            summary.attrs["region_id"]="synthetic"
            summary.to_netcdf(p/"summary.nc")
            cfg=dict(mod17_template=str(p/"gpp_{year}.nc"),mod17_years=[2015],cci_path=str(p/"absent.nc"),fluxcom_manifest=None)
            result=compare_region(p/"summary.nc",p/"output",cfg)
            status=[s for s in result["statuses"] if s["product"]=="MOD17A2HGF.061"][0]
            self.assertEqual(status["accepted_90"],2*46*4)
            import pandas as pd
            pairs=pd.read_csv(p/"output/reference_pairs.csv.gz")
            np.testing.assert_allclose(pairs.reference,8.)
            resumed=compare_region(p/"summary.nc",p/"output",cfg)
            self.assertEqual(result["completed_at"],resumed["completed_at"])
            (p/"output/reference_metrics.csv").unlink()
            rebuilt=compare_region(p/"summary.nc",p/"output",cfg)
            self.assertTrue((p/"output/reference_metrics.csv").is_file())
            self.assertNotEqual(rebuilt["completed_at"],resumed["completed_at"])


if __name__ == "__main__":
    unittest.main()
