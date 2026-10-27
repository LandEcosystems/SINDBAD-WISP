import xarray as xr
import pandas as pd

SRC = "/Users/skoirala/research/RnD/data/WISP/AUST-1_2019-11-06_2019-11-20_-32.0_-29.0_150.0_153.0_daily_grid_ent00_physical_hourly_soil_pft_rgpot_viirs_binary_wisp_firefrac.nc"
DST = SRC.replace("_hourly_", "_daily_")

# variables whose native meaning is an accumulated amount over the hour (precipitation,
# potential evaporation) or an already-converted hourly energy total (the SINDBAD f_rg,
# f_PAR, f_rn aliases, in MJ m-2 h-1). These need a daily SUM to give a correct daily total.
SUM_VARS = {"tp", "pev", "cp", "lsp", "f_rain", "f_rg", "f_PAR", "f_rn", "f_rg_pot"}

# VIIRS/fire detection variables where averaging across the day would dilute a real detection.
# A MAX preserves the strongest signal seen that day instead of washing it out with hours of
# no-fire/no-detection values.
MAX_VARS = {"active_fire", "frp", "fire_frac", "f_burnt_area"}

# every other time-varying variable is either an ERA5 instantaneous field, an hourly mean
# flux in W m-2, or a daily or composite product forward-filled onto the hourly grid, all of
# which need a daily MEAN to stay physically meaningful in the same units.

ds = xr.open_dataset(SRC)

daily_vars = {}
methods = {}
for v in ds.data_vars:
    da = ds[v]
    if "time" not in da.dims:
        daily_vars[v] = da
        methods[v] = "no time dimension, unchanged"
        continue
    if v in SUM_VARS:
        daily = da.resample(time="1D").sum(skipna=True, min_count=1)
        methods[v] = "sum"
    elif v in MAX_VARS:
        daily = da.resample(time="1D").max(skipna=True)
        methods[v] = "max"
    else:
        daily = da.resample(time="1D").mean(skipna=True)
        methods[v] = "mean"
    daily.attrs = dict(da.attrs)
    daily.attrs["daily_aggregation_method"] = methods[v]
    daily_vars[v] = daily

ds_daily = xr.Dataset(daily_vars, attrs=dict(ds.attrs))
ds_daily.attrs["history"] = ds.attrs.get("history", "") + f"; resampled from hourly to daily with wisp_to_daily.py on {pd.Timestamp.now().isoformat()}"

hours_per_day = ds["f_airT"].resample(time="1D").count(dim="time").isel(lat=0, lon=0).values
print("hours contributing to each day, first and last day are partial:")
for d, n in zip(pd.to_datetime(ds_daily.time.values).date, hours_per_day):
    print(f"  {d}: {int(n)} hours")

print()
print("aggregation method chosen per variable:")
for v, m in methods.items():
    print(f"  {v:<28} {m}")

ds_daily.to_netcdf(DST)
print()
print("wrote:", DST)
print(ds_daily)
