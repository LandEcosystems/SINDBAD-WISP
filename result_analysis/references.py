"""Reference products on their native spatial and temporal support.

This module never changes model files.  Formal pairs are coarse reference pixels,
not repeated fine pixels.  ``compare_region`` consumes the compact summaries made
by the analysis worker.  Reference failures remain explicit in ``statuses.json``.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
from collections import OrderedDict
from typing import Any, Iterable

import numpy as np
from scipy.sparse import csr_matrix

MOD17_TEMPLATE = os.environ.get("SINDBAD_MOD17_TEMPLATE", "/path/to/MOD17/Gpp.average.7200.3600.{year}.nc")
CCI_PATH = os.environ.get("SINDBAD_CCI_BIOMASS_PATH", "/path/to/CCI/AGB_annual.nc")
FLUXCOM_ROOT = (
    "https://swift.dkrz.de/v1/dkrz_a1e106384d7946408b9724b59858a536/"
    "fluxcom-x/FLUXCOMxBase/"
)
SCHEMA_VERSION = "reference-comparison-1.0.0"
PAIR_FIELDS = [
    "region_id", "scenario", "product", "variable", "period_start", "period_end",
    "days", "reference_cell_id", "reference_lat", "reference_lon", "model",
    "reference", "units", "covered_area_m2", "reference_support_area_m2",
    "coverage", "accepted_90", "accepted_80", "exclusion_reason", "carbon_fraction",
    "reference_lat_min", "reference_lat_max", "reference_lon_min", "reference_lon_max",
    "component", "reference_product", "model_value", "reference_value", "year",
    "group_type", "group", "support_scale",
]
ICOS_COLLECTIONS = {
    "GPP": "AYj7-lwcdCLnBXJDoscxQZou", "NEE": "X_-p994BqVSG6nWJkeK1ALjW",
    "ET": "_l85vWiIV81AifoxCkty50YI", "ET_T": "aCBG-AZIJtCCia7bhMYWin1-",
}


def date64(value: Any) -> np.datetime64:
    """Calendar day; discard noisy noon / microseconds in old IDL time axes."""
    if hasattr(value, "year"):
        return np.datetime64(f"{value.year:04d}-{value.month:02d}-{value.day:02d}", "D")
    return np.datetime64(str(value)[:10], "D")


def modis_intervals(year: int) -> list[tuple[np.datetime64, np.datetime64]]:
    start = np.datetime64(f"{year:04d}-01-01", "D")
    stop = np.datetime64(f"{year+1:04d}-01-01", "D")
    return [(s, min(s + np.timedelta64(8, "D"), stop))
            for s in np.arange(start, stop, np.timedelta64(8, "D"))]


def calendar_months(year: int) -> list[tuple[np.datetime64, np.datetime64]]:
    return [(np.datetime64(f"{year:04d}-{m:02d}-01"),
             np.datetime64(f"{year + (m == 12):04d}-{m % 12 + 1:02d}-01"))
            for m in range(1, 13)]


def integrate_daily(values: np.ndarray, dates: Iterable, intervals: Iterable,
                    required_mask: np.ndarray | None = None) -> np.ndarray:
    """Strict cumulative flux. Missing dates/values never become smaller totals."""
    x = np.asarray(values, dtype=np.float64)
    days = np.asarray([date64(v) for v in dates])
    if len(days) != x.shape[0] or len(np.unique(days)) != len(days):
        raise ValueError("Daily axis must have one unique date per array row")
    lookup = {int(d.astype(int)): i for i, d in enumerate(days)}
    result = []
    for begin, end in intervals:
        expected = np.arange(date64(begin), date64(end), np.timedelta64(1, "D"))
        ids = [lookup.get(int(d.astype(int))) for d in expected]
        if not len(ids) or any(i is None for i in ids):
            out = np.full(x.shape[1:], np.nan)
        else:
            selected = x[np.asarray(ids, dtype=int)]
            valid = np.isfinite(selected).all(axis=0)
            out = np.where(valid, np.sum(selected, axis=0, dtype=np.float64), np.nan)
        if required_mask is not None:
            out = np.where(required_mask, out, np.nan)
        result.append(out)
    return np.asarray(result)


def cell_bounds(centers: np.ndarray, bounds: np.ndarray | None = None) -> np.ndarray:
    c = np.asarray(centers, dtype=np.float64)
    if c.ndim != 1 or not np.isfinite(c).all() or len(np.unique(c)) != len(c):
        raise ValueError("Coordinates must be a finite, unique one-dimensional axis")
    if bounds is not None:
        b = np.asarray(bounds, dtype=np.float64)
        if b.shape != (len(c), 2) or not np.isfinite(b).all():
            raise ValueError("Coordinate bounds must be finite (n,2)")
        b = np.sort(b, axis=1)
    else:
        if len(c) < 2:
            raise ValueError("A single coordinate requires explicit bounds")
        diff = np.diff(c)
        if not (np.all(diff > 0) or np.all(diff < 0)):
            raise ValueError("Coordinate axis is not monotonic")
        edges = np.r_[c[0] - diff[0] / 2, (c[:-1] + c[1:]) / 2,
                      c[-1] + diff[-1] / 2]
        b = np.sort(np.column_stack((edges[:-1], edges[1:])), axis=1)
    if np.any(b[:, 1] <= b[:, 0]) or np.any(c < b[:, 0]) or np.any(c > b[:, 1]):
        raise ValueError("Invalid bounds or centers outside their cells")
    return b


def _lat_integral(latitude: np.ndarray) -> np.ndarray:
    """WGS84 ellipsoid surface integral between parallels, per longitude radian."""
    a = 6378137.0
    e2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
    e = math.sqrt(e2)
    u = np.sin(np.deg2rad(latitude))
    return a * a * (1 - e2) * (u / (2 * (1 - e2 * u * u)) + np.arctanh(e * u) / (2 * e))


def rectangle_area(lat_bounds: np.ndarray, lon_bounds: np.ndarray) -> np.ndarray:
    lat = np.asarray(lat_bounds, dtype=float)
    lon = np.asarray(lon_bounds, dtype=float)
    if np.any(np.abs(lat) > 90) or np.any(lon[..., 1] - lon[..., 0] > 180):
        raise ValueError("Invalid latitude or unsupported antimeridian rectangle")
    return (_lat_integral(lat[..., 1]) - _lat_integral(lat[..., 0])) * np.deg2rad(lon[..., 1] - lon[..., 0])


def overlap_matrix(model_lat: np.ndarray, model_lon: np.ndarray,
                   reference_lat: np.ndarray, reference_lon: np.ndarray,
                   *, model_lat_bounds=None, model_lon_bounds=None,
                   reference_lat_bounds=None, reference_lon_bounds=None):
    """CSR matrix [reference pixel, model pixel] of true rectangular areas.

    Both axes may ascend or descend.  This deliberately uses parallels/meridians,
    the actual geometry of regular geographic rasters, rather than treating pixel
    corners as four long geodesics. No interpolation of intensities occurs.
    """
    ml = cell_bounds(model_lat, model_lat_bounds)
    mo = cell_bounds(model_lon, model_lon_bounds)
    rl = cell_bounds(reference_lat, reference_lat_bounds)
    ro = cell_bounds(reference_lon, reference_lon_bounds)
    rows, cols, areas = [], [], []
    for rlat, (lo, hi) in enumerate(rl):
        iy = np.flatnonzero((ml[:, 1] > lo) & (ml[:, 0] < hi))
        if not len(iy):
            continue
        bounds_y = np.column_stack((np.maximum(ml[iy, 0], lo), np.minimum(ml[iy, 1], hi)))
        sy = _lat_integral(bounds_y[:, 1]) - _lat_integral(bounds_y[:, 0])
        for rlon, (west, east) in enumerate(ro):
            ix = np.flatnonzero((mo[:, 1] > west) & (mo[:, 0] < east))
            if not len(ix):
                continue
            sx = np.deg2rad(np.minimum(mo[ix, 1], east) - np.maximum(mo[ix, 0], west))
            yy, xx = np.meshgrid(iy, ix, indexing="ij")
            rows.extend([rlat * len(ro) + rlon] * yy.size)
            cols.extend((yy * len(mo) + xx).ravel().tolist())
            areas.extend((sy[:, None] * sx[None, :]).ravel().tolist())
    matrix = csr_matrix((areas, (rows, cols)), shape=(len(rl) * len(ro), len(ml) * len(mo)))
    ref_area = rectangle_area(rl[:, None, :], ro[None, :, :]).ravel()
    return matrix, ref_area


def aggregate_to_reference(values: np.ndarray, matrix: csr_matrix,
                           reference_support_area: np.ndarray,
                           valid_mask: np.ndarray, support_fraction=None):
    """Area-weighted means and coverage, with finite support per time/period.

    Reference support area is explicit. Unknown land fraction is never invented.
    Using whole reference-pixel area is a conservative coverage screen, not a
    claim to know its exact land support.
    """
    x = np.asarray(values, dtype=float)
    n = matrix.shape[1]
    flat = x.reshape((-1, n))
    mask = np.asarray(valid_mask, bool).reshape(n)
    fraction = np.ones(n) if support_fraction is None else np.asarray(support_fraction, float).reshape(n)
    if not np.isfinite(fraction).all() or np.any((fraction < 0) | (fraction > 1)):
        raise ValueError("Support fractions must be finite and in [0,1]")
    w = fraction * mask
    finite = np.isfinite(flat)
    denominators = np.asarray(matrix.dot((finite * w).T).T)
    numerators = np.asarray(matrix.dot((np.where(finite, flat, 0) * w).T).T)
    mean = np.divide(numerators, denominators, out=np.full_like(numerators, np.nan), where=denominators > 0)
    support = np.asarray(reference_support_area, dtype=float).reshape(1, -1)
    coverage = np.divide(denominators, support, out=np.zeros_like(denominators), where=support > 0)
    if np.nanmax(coverage, initial=0) > 1 + 1e-8:
        raise ValueError("Model overlap exceeds reference support: check area denominator")
    shape = x.shape[:-2] + (matrix.shape[0],)
    return mean.reshape(shape), coverage.reshape(shape), denominators.reshape(shape)


def weighted_metrics(model, reference, weight=None) -> dict[str, Any]:
    m, r = np.asarray(model, float).ravel(), np.asarray(reference, float).ravel()
    w = np.ones_like(m) if weight is None else np.asarray(weight, float).ravel()
    ok = np.isfinite(m) & np.isfinite(r) & np.isfinite(w) & (w > 0)
    m, r, w = m[ok], r[ok], w[ok]
    result = dict(n_pairs=int(len(m)), weight_sum=float(w.sum()), bias=None, mae=None,
                  rmse=None, pearson_r=None, r_squared=None, nse=None)
    if not len(m):
        return result
    w = w / w.sum()
    err = m - r
    dm, dr = m - np.sum(w * m), r - np.sum(w * r)
    vm, vr = np.sum(w * dm * dm), np.sum(w * dr * dr)
    result.update(bias=float(np.sum(w * err)), mae=float(np.sum(w * np.abs(err))),
                  rmse=float(np.sqrt(np.sum(w * err * err))))
    if vr > 0:
        result["nse"] = float(1 - np.sum(w * err * err) / vr)
    if vm > 0 and vr > 0:
        result["pearson_r"] = float(np.clip(np.sum(w * dm * dr) / math.sqrt(vm * vr), -1, 1))
        result["r_squared"] = result["pearson_r"] ** 2
    return result


def _identity(path: str | Path) -> dict:
    p = Path(path)
    result = {"path": str(p)}
    try:
        st = p.stat()
        result.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
    except OSError as exc:
        result["error"] = str(exc)
    return result


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _atomic_json(path: Path, value: Any):
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n")
    os.replace(tmp, path)


def _subset_indices(centers, extent):
    b = cell_bounds(centers)
    ids = np.flatnonzero((b[:, 1] > extent[0]) & (b[:, 0] < extent[1]))
    if not len(ids):
        raise ValueError("Reference and model domains do not intersect")
    return slice(int(ids.min()), int(ids.max()) + 1), b[ids]


def _read_nc_patch(path, variable, model_lat, model_lon):
    """Decode an audited NetCDF reference, reading only the spatial subset."""
    from netCDF4 import Dataset, num2date
    with Dataset(path) as ds:
        lat_name = next((s for s in ("lat", "latitude") if s in ds.variables), None)
        lon_name = next((s for s in ("lon", "longitude") if s in ds.variables), None)
        if not lat_name or not lon_name:
            raise ValueError("Reference lacks latitude/longitude coordinates")
        lats, lons = np.asarray(ds[lat_name][:]), np.asarray(ds[lon_name][:])
        ys, yb = _subset_indices(lats, (cell_bounds(model_lat).min(), cell_bounds(model_lat).max()))
        xs, xb = _subset_indices(lons, (cell_bounds(model_lon).min(), cell_bounds(model_lon).max()))
        v = ds[variable]
        ix = tuple(ys if d == lat_name else xs if d == lon_name else slice(None) for d in v.dimensions)
        arr = np.asarray(np.ma.filled(v[ix], np.nan), float)
        dimensions = list(v.dimensions)
        for i in reversed(range(len(dimensions))):
            if dimensions[i] not in ("time", lat_name, lon_name):
                if arr.shape[i] != 1:
                    raise ValueError("Unknown non-singleton reference dimension " + dimensions[i])
                arr = np.take(arr, 0, axis=i)
                dimensions.pop(i)
        arr = np.transpose(arr, [dimensions.index(k) for k in ("time", lat_name, lon_name)])
        time = ds["time"]
        dates = [date64(t) for t in num2date(time[:], time.units, getattr(time, "calendar", "standard"))]
        attrs = {k: str(v.getncattr(k)) for k in v.ncattrs()}
        return dict(values=arr, lat=lats[ys], lon=lons[xs], lat_bounds=yb, lon_bounds=xb,
                    dates=np.asarray(dates), attrs=attrs,
                    row_offset=int(getattr(ds, "source_row_offset", 0)) + ys.start,
                    col_offset=int(getattr(ds, "source_col_offset", 0)) + xs.start,
                    total_columns=int(getattr(ds, "source_grid_columns", len(lons))), source=_identity(path))


def _summary_axis(ds, prefix, axis):
    if prefix + "_start" in ds and prefix + "_end" in ds:
        return [date64(x) for x in ds[prefix + "_start"].values], [date64(x) for x in ds[prefix + "_end"].values]
    values = ds[axis].values
    starts = [date64(v) for v in values]
    if prefix == "month":
        ends = [d.astype("datetime64[M]") + np.timedelta64(1, "M") for d in starts]
    elif prefix == "composite":
        ends = [min(d + np.timedelta64(8, "D"), np.datetime64(str(d)[:4] + "-12-31") + np.timedelta64(1, "D")) for d in starts]
    else:
        raise ValueError("Explicit period bounds are required")
    return starts, [date64(v) for v in ends]


def _period_lookup(ds, prefix, axis):
    starts, ends = _summary_axis(ds, prefix, axis)
    return {(str(a), str(b)): i for i, (a, b) in enumerate(zip(starts, ends))}


def _component(ds, name, index, axis):
    if name not in ds:
        raise KeyError("Compact summary does not contain " + name)
    da = ds[name].isel({axis: index})
    return np.asarray(da.transpose("scenario", "lat", "lon").values, dtype=float)


def _pairs_for_period(region_id, scenarios, product, variable, begin, end, model, reference,
                      patch, matrix, support_area, mask, support_fraction, units, carbon_fraction=""):
    means, coverage, area = aggregate_to_reference(model, matrix, support_area, mask, support_fraction)
    ref = np.asarray(reference, float).ravel()
    ref_lat, ref_lon = np.meshgrid(patch["lat"], patch["lon"], indexing="ij")
    rows, cols = np.indices(ref_lat.shape)
    cellids = ((rows + patch["row_offset"]) * patch["total_columns"] + cols + patch["col_offset"]).ravel()
    for si, scenario in enumerate(scenarios):
        for j in range(len(ref)):
            reason = ""
            if not np.isfinite(ref[j]):
                reason = "reference_missing"
            elif not np.isfinite(means[si, j]):
                reason = "model_missing_or_no_overlap"
            elif coverage[si, j] < .9 - 1e-10:
                reason = "reference_support_coverage_below_90pct"
            finite = np.isfinite(ref[j]) and np.isfinite(means[si, j])
            yield dict(region_id=region_id, scenario=str(scenario), product=product, variable=variable,
                       period_start=str(begin), period_end=str(end), days=int((end - begin).astype(int)),
                       reference_cell_id=int(cellids[j]), reference_lat=float(ref_lat.ravel()[j]),
                       reference_lon=float(ref_lon.ravel()[j]), model=float(means[si, j]), reference=float(ref[j]),
                       units=units, covered_area_m2=float(area[si, j]),
                       reference_support_area_m2=float(support_area[j]), coverage=float(coverage[si, j]),
                       accepted_90=int(finite and coverage[si, j] >= .9 - 1e-10),
                       accepted_80=int(finite and coverage[si, j] >= .8 - 1e-10), exclusion_reason=reason,
                       carbon_fraction=carbon_fraction,
                       reference_lat_min=float(patch["lat_bounds"][rows.ravel()[j], 0]),
                       reference_lat_max=float(patch["lat_bounds"][rows.ravel()[j], 1]),
                       reference_lon_min=float(patch["lon_bounds"][cols.ravel()[j], 0]),
                       reference_lon_max=float(patch["lon_bounds"][cols.ravel()[j], 1]),
                       component=variable, reference_product=product,
                       model_value=float(means[si,j]), reference_value=float(ref[j]), year=int(str(begin)[:4]),
                       group_type="overall", group="all", support_scale="native reference grid")


def _matrix(ds, patch):
    return overlap_matrix(ds.lat.values, ds.lon.values, patch["lat"], patch["lon"],
                          reference_lat_bounds=patch["lat_bounds"], reference_lon_bounds=patch["lon_bounds"])


def mod17_pairs(ds, region_id, template=MOD17_TEMPLATE, years=range(2014, 2022)):
    look = _period_lookup(ds, "composite", "composite")
    for year in years:
        path = template.format(year=year)
        patch = _read_nc_patch(path, "Gpp", ds.lat.values, ds.lon.values)
        if not patch["attrs"].get("units", "").lower().startswith("kg"):
            raise ValueError("MOD17 decoded units must be kg C m-2 per composite")
        if float(patch["attrs"].get("scale_factor", 1)) != 1:
            raise ValueError("This adapter requires the audited already-decoded MOD17 product")
        matrix, area = _matrix(ds, patch)
        intervals = dict((str(a), (a, b)) for a, b in modis_intervals(year))
        for source_index, day in enumerate(patch["dates"]):
            if str(day) not in intervals:
                raise ValueError("Unexpected MOD17 composite date: " + str(day))
            begin, end = intervals[str(day)]
            key = (str(begin), str(end))
            if key not in look:
                continue
            model = _component(ds, "gpp_modis_sum", look[key], "composite")
            # The input is already scaled to kg C, so only kg -> g remains.
            reference = patch["values"][source_index] * 1000.0
            yield from _pairs_for_period(region_id, ds.scenario.values, "MOD17A2HGF.061", "gpp",
                                         begin, end, model, reference, patch, matrix, area,
                                         ds.run_valid_mask.values, None, "gC m-2 composite-1")


def cci_pairs(ds, region_id, path=CCI_PATH, carbon_fractions=(.47, .51)):
    if "forest_fraction" not in ds:
        raise ValueError("Original-2019 forest support is required for CCI wood comparisons")
    patch = _read_nc_patch(path, "agb", ds.lat.values, ds.lon.values)
    if patch["attrs"].get("units", "").replace(" ", "").lower() not in ("mg/ha", "mgha-1"):
        raise ValueError("CCI units must be Mg dry biomass ha-1")
    matrix, area = _matrix(ds, patch)
    years = {int(y): i for i, y in enumerate(ds.year.values)}
    for j, date in enumerate(patch["dates"]):
        year = int(str(date)[:4])
        if year not in years or year == 2014:
            continue
        model = _component(ds, "cVegWood_annual_mean", years[year], "year")
        begin, end = np.datetime64(f"{year}-01-01"), np.datetime64(f"{year+1}-01-01")
        for cf in carbon_fractions:
            reference = patch["values"][j] * 100 * cf
            yield from _pairs_for_period(region_id, ds.scenario.values, "ESA_CCI_Biomass_v6", "cVegWood",
                                         begin, end, model, reference, patch, matrix, area,
                                         ds.run_valid_mask.values, ds.forest_fraction.values,
                                         "gC m-2", cf)


def fluxcom_hourly_to_monthly(values, variable, hour_axis=1):
    """Convert verified mean-hour-of-day monthly X-BASE values to monthly daily rates.

    This helper requires all 24 hours, and outputs gC m-2 day-1 or mm day-1.
    The caller multiplies by the actual number of days for monthly totals.
    It deliberately does not accept unitless, unverified ``old_aggs`` stores.
    """
    x = np.asarray(values, dtype=float)
    if x.shape[hour_axis] != 24:
        raise ValueError("FLUXCOM conversion requires all 24 hourly bins")
    valid = np.isfinite(x).all(axis=hour_axis)
    scale = 3600 * 12e-6 if variable in ("GPP", "NEE") else 1.0
    if variable not in ("GPP", "NEE", "ET", "ET_T"):
        raise ValueError("Unknown FLUXCOM variable")
    return np.where(valid, np.sum(x, axis=hour_axis, dtype=float) * scale, np.nan)


def official_monthly_to_daily_rate(values, variable, units):
    """Respect units in the published monthly NetCDF and ICOS variable metadata."""
    x=np.asarray(values,dtype=float)
    if variable in ("GPP","NEE") and units in ("gC m-2 d-1","gC m-2 day-1"):
        return x,1.0
    if variable in ("ET","ET_T"):
        if units in ("mm d-1","mm day-1"):
            return x,1.0
        if units in ("mm hr-1","mm h-1"):
            return x*24.0,24.0
    raise ValueError("Unexpected official monthly units: "+units)


def fluxcom_verified_monthly_pairs(ds, region_id, manifest_path):
    """Read an explicit official-source monthly cache, never unknown old_aggs.

    The manifest must bind cache SHA256, original official URL and physical units.
    Cache variables are monthly mean daily rates on their native latitude grid.
    Their values must have been generated from official hourly data with strict
    complete-day and complete-month aggregation (or equivalent official monthly
    totals).  ``prepare_fluxcom_monthly`` implements the hourly path below.
    """
    import xarray as xr
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get("product") != "FLUXCOM-X X-BASE v1.0" or not manifest.get("complete_months_only"):
        raise ValueError("Unverified FLUXCOM cache provenance / incomplete temporal support")
    look = _period_lookup(ds, "month", "month")
    mapping = {"GPP": ("gpp", "gpp_monthly_sum", "gC m-2 day-1"),
               "NEE": ("nee", "nee_monthly_sum", "gC m-2 day-1"),
               "ET": ("evapotranspiration", "evapotranspiration_monthly_sum", "mm day-1"),
               "ET_T": ("transpiration", "transpiration_monthly_sum", "mm day-1")}
    for record in manifest["files"]:
        v = record["variable"]
        official = (record["source_url"].startswith(FLUXCOM_ROOT + v) or
                    record["source_url"].startswith("https://data.icos-cp.eu/objects/"))
        if v not in mapping or not official:
            raise ValueError("FLUXCOM source does not match verified official store")
        file = Path(record["path"])
        if not file.is_absolute():
            file = Path(manifest_path).parent / file
        actual = hashlib.sha256(file.read_bytes()).hexdigest()
        if actual != record["sha256"]:
            raise ValueError("FLUXCOM cache checksum mismatch")
        variable, name, expected_units = mapping[v]
        patch = _read_nc_patch(file, v, ds.lat.values, ds.lon.values)
        if patch["attrs"].get("units") != expected_units:
            raise ValueError("FLUXCOM monthly cache unit mismatch")
        matrix, area = _matrix(ds, patch)
        for j, begin in enumerate(patch["dates"]):
            end = date64(begin.astype("datetime64[M]") + np.timedelta64(1, "M"))
            key = (str(begin), str(end))
            if key not in look:
                continue
            model = _component(ds, name, look[key], "month")
            # Compact monthly sums can include a shortened first simulation
            # month. They are not comparable to a full reference month.
            if begin < date64(ds.attrs.get("simulation_start", "2014-01-12")) or end > date64(ds.attrs.get("simulation_end_exclusive", "2024-01-01")):
                model[:] = np.nan
            reference = patch["values"][j] * int((end - begin).astype(int))
            units = "gC m-2 month-1" if v in ("GPP", "NEE") else "mm month-1"
            yield from _pairs_for_period(region_id, ds.scenario.values, "FLUXCOM-X_X-BASE_v1.0", variable,
                                         begin, end, model, reference, patch, matrix, area,
                                         ds.run_valid_mask.values, None, units)


class _HTTPRangeReader(io.RawIOBase):
    """Seekable, bounded-memory range reader for public ICOS NetCDF objects."""
    def __init__(self, url, size, object_id, block_size=1024 * 1024, max_blocks=32):
        self.url, self.size, self.object_id = url, int(size), object_id
        self.block_size, self.max_blocks = block_size, max_blocks
        self.position = 0
        self.cache = OrderedDict()
        self.downloaded_bytes = 0
        self.requests = 0
        self.etag = None

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        value = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if value < 0:
            raise ValueError("Negative seek")
        self.position = value
        return value

    def _block(self, number):
        from urllib.request import Request, urlopen
        if number in self.cache:
            self.cache.move_to_end(number)
            return self.cache[number]
        begin = number * self.block_size
        end = min(self.size - 1, begin + self.block_size - 1)
        request = Request(self.url, headers={"Range": f"bytes={begin}-{end}",
                                             "Cookie": "CpLicenseAcceptedFor=" + self.object_id,
                                             "User-Agent": "SINDBAD-WISP reference analysis (CC-BY attribution retained)"})
        for attempt in range(3):
            try:
                with urlopen(request, timeout=90) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {begin}-{end}/{self.size}":
                        raise OSError("Server did not honor exact HTTP range; refusing global/HTML download")
                    etag = response.headers.get("ETag")
                    if self.etag is not None and etag != self.etag:
                        raise ValueError("Remote object identity changed during extraction")
                    self.etag = etag
                    data = response.read(end - begin + 2)
                if len(data) != end - begin + 1:
                    raise OSError("Truncated or oversized HTTP range")
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        self.downloaded_bytes += len(data)
        self.requests += 1
        self.cache[number] = data
        while len(self.cache) > self.max_blocks:
            self.cache.popitem(last=False)
        return data

    def read(self, size=-1):
        count = self.size - self.position if size < 0 else min(size, self.size - self.position)
        pieces = []
        while count > 0:
            number, offset = divmod(self.position, self.block_size)
            block = self._block(number)
            take = min(count, len(block) - offset)
            if take <= 0:
                raise OSError("Unexpected end of ranged reference object")
            pieces.append(block[offset:offset+take])
            self.position += take
            count -= take
        return b"".join(pieces)

    def readinto(self, target):
        value = self.read(len(target))
        target[:len(value)] = value
        return len(value)


def _get_json(url):
    from urllib.request import Request, urlopen
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=60) as response:
        return json.load(response)


def validate_official_monthly_cache(path, variable, year, expected=None):
    """Decode every cached chunk before publication or reuse; a hash is insufficient."""
    from netCDF4 import Dataset,num2date
    with Dataset(path) as source:
        values=np.asarray(np.ma.filled(source[variable][:],np.nan),dtype=np.float32)
        if values.ndim!=3 or values.shape[0]!=12:
            raise ValueError("Official monthly cache does not contain all twelve months")
        dates=[date64(d) for d in num2date(source["time"][:],source["time"].units,getattr(source["time"],"calendar","standard"))]
        if dates!=[a for a,b in calendar_months(year)]:
            raise ValueError("Official cache dates differ from complete calendar months")
        units="gC m-2 day-1" if variable in ("GPP","NEE") else "mm day-1"
        if source[variable].units!=units:
            raise ValueError("Official monthly cache units differ from the verified daily-rate convention")
        if expected is not None and not np.array_equal(values,np.asarray(expected,dtype=np.float32),equal_nan=True):
            raise ValueError("Lossless official cache read-back differs from prepared values")
    return {"full_array_decoded":True,"shape":list(values.shape),"finite_count":int(np.isfinite(values).sum())}


def prepare_fluxcom_official_monthly(variable, year, bbox, destination):
    """Fetch only a geographic crop of one official 0.05-degree monthly file.

    ICOS serves CC-BY-4.0 NetCDFs with HTTP range support. Source object metadata,
    ETag, source hash and its physical units are retained; only the cropped output
    receives a locally verified SHA256 (we do not claim a whole-file rehash).
    """
    import h5netcdf
    import xarray as xr
    from netCDF4 import num2date
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    filename = f"{variable}_{year}_monthly_verified.nc"
    record_path = destination / f"{variable}_{year}_provenance.json"
    target = destination / filename
    identity = dict(variable=variable, year=int(year), bbox=list(map(float, bbox)), schema=SCHEMA_VERSION)
    if record_path.exists() and target.exists():
        record = json.loads(record_path.read_text())
        if record.get("request") == identity and hashlib.sha256(target.read_bytes()).hexdigest() == record.get("sha256"):
            validate_official_monthly_cache(target,variable,year)
            return record
    if variable not in ICOS_COLLECTIONS:
        raise ValueError("Unknown FLUXCOM variable")
    collection = _get_json("https://meta.icos-cp.eu/collections/" + ICOS_COLLECTIONS[variable])
    members = [r for r in collection["members"] if str(year) in r.get("title", r.get("name", ""))]
    if len(members) != 1:
        raise ValueError("Official collection does not identify a unique year")
    yearly = _get_json(members[0]["res"])
    members = [r for r in yearly["members"] if "monthly" in r.get("name", "").lower()
               and "0.05" in r.get("name", "") and "diurnal" not in r.get("name", "").lower()]
    if len(members) != 1:
        raise ValueError("Official collection does not identify unique 0.05-degree monthly data")
    metadata = _get_json(members[0]["res"])
    url = metadata["accessUrl"]
    object_id = url.rsplit("/", 1)[-1]
    reader = _HTTPRangeReader(url, metadata["size"], object_id)
    west, south, east, north = map(float, bbox)
    with h5netcdf.File(reader, "r") as source:
        v = source[variable]
        units = str(v.attrs.get("units", ""))
        _,unit_multiplier=official_monthly_to_daily_rate(np.asarray([0.]),variable,units)
        if tuple(v.dimensions) != ("time", "lat", "lon") or v.shape[0] != 12:
            raise ValueError("Unexpected official monthly shape")
        lat, lon = np.asarray(source["lat"][:]), np.asarray(source["lon"][:])
        ys, ybounds = _subset_indices(lat, (south, north))
        xs, xbounds = _subset_indices(lon, (west, east))
        values = np.asarray(v[:, ys, xs], dtype=np.float32)
        # Inspect explicit missing codes. NaN is not converted to a no-flux zero.
        fill = v.attrs.get("_FillValue", np.nan)
        if np.isfinite(fill):
            values[values == fill] = np.nan
        values=(values.astype(np.float64)*unit_multiplier).astype(np.float32)
        t = source["time"]
        days = [date64(d) for d in num2date(t[:], str(t.attrs["units"]), str(t.attrs.get("calendar", "standard")))]
        if {str(d)[:7] for d in days} != {f"{year}-{m:02d}" for m in range(1, 13)}:
            raise ValueError("Official monthly timestamps are incomplete or duplicated")
        starts = [date64(d.astype("datetime64[M]")) for d in days]
        if "time_bnds" in source:
            raw_bounds = np.asarray(source["time_bnds"][:])
            bounds = np.asarray([date64(d) for d in num2date(raw_bounds.ravel(), str(t.attrs["units"]), str(t.attrs.get("calendar", "standard")))]).reshape(12, 2)
            for index, (begin, end) in enumerate(calendar_months(year)):
                if starts[index] != begin or bounds[index, 0] != begin or bounds[index, 1] != end:
                    raise ValueError("Official monthly time bounds do not match complete calendar months")
        else:
            raise ValueError("Official monthly reference lacks interval bounds")
        land_fraction = np.asarray(source["land_fraction"][ys, xs], dtype=np.float32) if "land_fraction" in source else None
        attrs = {str(k): str(value) for k, value in source.attrs.items()}
        vattrs = {str(k): str(value) for k, value in v.attrs.items()}
    result = xr.Dataset({variable: (("time", "latitude", "longitude"), values),
                         "time_bounds": (("time", "bounds"), bounds),
                         "latitude_bounds": (("latitude", "bounds"), ybounds),
                         "longitude_bounds": (("longitude", "bounds"), xbounds)},
                        coords={"time": starts, "latitude": lat[ys], "longitude": lon[xs]})
    result[variable].attrs.update(units="gC m-2 day-1" if variable in ("GPP", "NEE") else "mm day-1",
                                  source_url=url, temporal_semantics="official monthly mean daily rate")
    result.time.attrs["bounds"] = "time_bounds"
    if land_fraction is not None:
        result["reference_land_fraction"] = (("latitude", "longitude"), land_fraction)
        result.reference_land_fraction.attrs["description"] = "fraction of land for which product is defined; retained for audit, not multiplied into rate"
    result.attrs.update(product="FLUXCOM-X X-BASE v1.0", source_metadata_url=members[0]["res"],
                        licence="ICOS RI, licensed under CC BY 4.0", changes=f"geographic crop; source rates multiplied by {unit_multiplier} to daily rates; no land_fraction scaling",
                        source_grid_rows=len(lat), source_grid_columns=len(lon), source_row_offset=ys.start, source_col_offset=xs.start)
    temp = target.with_suffix(".nc.partial")
    result.to_netcdf(temp, encoding={variable: {"zlib": True, "complevel": 4},
                                    "time": {"units": "days since 2001-01-01", "calendar": "proleptic_gregorian"},
                                    "time_bounds": {"units": "days since 2001-01-01", "calendar": "proleptic_gregorian"}})
    validation=validate_official_monthly_cache(temp,variable,year,values)
    os.replace(temp, target)
    record = dict(validation=validation,variable=variable, path=str(target.resolve()), source_url=url, source_metadata_url=members[0]["res"],
                  source_hash=metadata["hash"], source_size=metadata["size"], source_etag=reader.etag,
                  downloaded_bytes=reader.downloaded_bytes, http_requests=reader.requests,
                  sha256=hashlib.sha256(target.read_bytes()).hexdigest(), request=identity,
                  source_attributes=attrs, variable_attributes=vattrs, units=result[variable].attrs["units"],
                  source_to_daily_rate_multiplier=unit_multiplier,
                  licence="ICOS RI, licensed under CC BY 4.0", official_metadata=metadata)
    _atomic_json(record_path, record)
    return record


def prepare_fluxcom_monthly(variable, bbox, destination, start="2014-01-01", end="2022-01-01"):
    """Explicit opt-in network job: create audited regional monthly cache.

    Official Zarr chunks are large. Run on a compute job with network/cache access,
    and reuse this cache across all overlapping cubes. This is never implicitly
    invoked by compare_region and does not fall back to unverified local stores.
    """
    import xarray as xr
    if variable not in ("GPP", "NEE", "ET", "ET_T"):
        raise ValueError("Unknown FLUXCOM variable")
    west, south, east, north = map(float, bbox)
    source = FLUXCOM_ROOT + variable
    ds = xr.open_zarr(source, consolidated=True)
    try:
        data = ds[variable]
        unit = str(data.attrs.get("units", ""))
        expected = "umol" if variable in ("GPP", "NEE") else "mm"
        if expected not in unit or (variable in ("GPP", "NEE") and ("s" not in unit or "CO2" not in unit)):
            raise ValueError("Unexpected official FLUXCOM physical units: " + unit)
        # Coordinate direction is preserved; compare_region handles both.
        lat_name = "lat" if "lat" in ds.coords else "latitude"
        lon_name = "lon" if "lon" in ds.coords else "longitude"
        yids = np.flatnonzero((ds[lat_name].values >= south - .05) & (ds[lat_name].values <= north + .05))
        xids = np.flatnonzero((ds[lon_name].values >= west - .05) & (ds[lon_name].values <= east + .05))
        if not len(yids) or not len(xids):
            raise ValueError("FLUXCOM bbox has no native cells")
        da = data.isel({lat_name: slice(yids.min(), yids.max()+1), lon_name: slice(xids.min(), xids.max()+1)})
        da = da.sel(time=slice(start, str(date64(end) - np.timedelta64(1, "D"))))
        if "hour" not in da.dims or da.sizes["hour"] != 24:
            raise ValueError("Official store must expose a complete 24-hour daily axis")
        # One month at a time: final reference is small; source chunk cache should
        # be supplied by fsspec/storage layer on clusters without repeated reads.
        records, dates = [], []
        months = np.arange(np.datetime64(start, "M"), np.datetime64(end, "M"), np.timedelta64(1, "M"))
        for month in months:
            begin, stop = month.astype("datetime64[D]"), (month + np.timedelta64(1, "M")).astype("datetime64[D]")
            part = da.sel(time=slice(str(begin), str(stop - np.timedelta64(1, "D"))))
            expected_days = np.arange(begin, stop)
            actual_days = np.asarray([date64(t) for t in part.time.values])
            if not np.array_equal(actual_days, expected_days):
                raise ValueError("Incomplete FLUXCOM month: " + str(month))
            x = part.transpose("time", "hour", lat_name, lon_name).values
            rates = fluxcom_hourly_to_monthly(x, variable)
            complete = np.isfinite(rates).all(axis=0)
            records.append(np.where(complete, rates.mean(axis=0), np.nan))
            dates.append(begin)
        units = "gC m-2 day-1" if variable in ("GPP", "NEE") else "mm day-1"
        result = xr.Dataset({variable: (("time", "latitude", "longitude"), np.asarray(records, np.float32))},
                            coords={"time": dates, "latitude": da[lat_name].values, "longitude": da[lon_name].values})
        result[variable].attrs.update(units=units, source_url=source, temporal_semantics="mean daily rate over complete month")
        result.attrs.update(product="FLUXCOM-X X-BASE v1.0", area_basis="native product; land_fraction scaling not applied")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / (variable + "_monthly_verified.nc")
        temporary = path.with_suffix(".nc.partial")
        result.to_netcdf(temporary, encoding={variable: {"zlib": True, "complevel": 4}})
        os.replace(temporary, path)
        rec = dict(variable=variable, path=str(path.resolve()), source_url=source,
                   sha256=hashlib.sha256(path.read_bytes()).hexdigest(), units=units)
        manifest = dict(product="FLUXCOM-X X-BASE v1.0", complete_months_only=True, bbox=list(bbox),
                        period=[start, end], files=[rec], source_attrs={k: str(v) for k,v in ds.attrs.items()})
        _atomic_json(destination / (variable + "_manifest.json"), manifest)
        return manifest
    finally:
        ds.close()


def metrics_from_pairs(rows):
    """Area-weighted metrics per product/scenario and actual year; not HT totals."""
    groups = {}
    for row in rows:
        for threshold in (90, 80):
            if not int(row["accepted_" + str(threshold)]):
                continue
            for year in (str(row["period_start"])[:4], "all_available"):
                key = tuple(row[k] for k in ("region_id", "scenario", "product", "variable", "units", "carbon_fraction")) + (year, threshold)
                groups.setdefault(key, []).append(row)
    result = []
    for key, values in sorted(groups.items(), key=lambda item: str(item[0])):
        metric = weighted_metrics([v["model"] for v in values], [v["reference"] for v in values],
                                  [v["covered_area_m2"] for v in values])
        metric.update(dict(zip(("region_id", "scenario", "product", "variable", "units", "carbon_fraction", "year", "minimum_coverage_pct"), key)))
        metric.update(n_reference_cells=len({v["reference_cell_id"] for v in values}),
                      n_periods=len({v["period_start"] for v in values}),
                      statistics_support="reference grid; per-period cumulative flux or annual mean stock",
                      weighting="effective overlap area; no inclusion-probability or vegetation-fraction scaling")
        result.append(metric)
    return result


def annualize_reference_pairs(rows):
    """Cumulative fluxes over complete calendar years on unchanged native cells.

    Annual mean wood stocks are retained unchanged. Missing periods do not turn
    into partial-year annual totals. Common-product rankings should additionally
    inner-join these rows on coordinates and year, not compare different supports.
    """
    groups={}
    for row in rows:
        key=tuple(row[k] for k in ("region_id","scenario","product","variable","reference_cell_id","carbon_fraction"))+(int(str(row["period_start"])[:4]),)
        groups.setdefault(key,[]).append(row)
    result=[]
    for key,records in groups.items():
        records=sorted(records,key=lambda r:r["period_start"])
        row=dict(records[0]);year=key[-1]
        if row["variable"]=="cVegWood":
            if len(records)!=1:
                raise ValueError("Duplicate annual stock reference")
            result.append(row);continue
        first,last=f"{year}-01-01",f"{year+1}-01-01"
        complete=(records[0]["period_start"]==first and records[-1]["period_end"]==last and
                  all(a["period_end"]==b["period_start"] for a,b in zip(records[:-1],records[1:])))
        complete=complete and all(np.isfinite(r["model"]) and np.isfinite(r["reference"]) for r in records)
        row.update(period_start=first,period_end=last,days=366 if calendar.isleap(year) else 365,
                   model=sum(r["model"] for r in records) if complete else np.nan,
                   reference=sum(r["reference"] for r in records) if complete else np.nan,
                   coverage=min(r["coverage"] for r in records),covered_area_m2=min(r["covered_area_m2"] for r in records),
                   accepted_90=int(complete and all(r["accepted_90"] for r in records)),
                   accepted_80=int(complete and all(r["accepted_80"] for r in records)),
                   exclusion_reason="" if complete and all(r["accepted_90"] for r in records) else
                   ("incomplete_calendar_year_or_missing_pair" if not complete else "reference_support_coverage_below_90pct"),
                   units="mm year-1" if row["variable"] in ("evapotranspiration","transpiration") else "gC m-2 year-1")
        row["model_value"],row["reference_value"]=row["model"],row["reference"]
        result.append(row)
    return result


def common_annual_gpp_pairs(rows, products=("MOD17A2HGF.061", "FLUXCOM-X_X-BASE_v1.0"), years=range(2015,2022)):
    """Exact common years/native 0.05-degree cells for multi-reference GPP.

    Cross-cube duplicates prefer coverage, then stable region ID, never error.
    Cell identities use their coordinates because products can store latitude
    in opposite orders. Values not on the canonical 0.05-degree grid are rejected.
    Returns the long-form intersection, one row per product for each common pair.
    """
    chosen={}
    for original in rows:
        row=dict(original)
        product=row.get("product",row.get("reference_product"))
        if product not in products or row.get("variable")!="gpp" or not int(float(row.get("accepted_90",0))):
            continue
        year=int(str(row["period_start"])[:4])
        if year not in years or row["period_start"]!=f"{year}-01-01" or row["period_end"]!=f"{year+1}-01-01":
            continue
        lat,lon=float(row["reference_lat"]),float(row["reference_lon"])
        iy,ix=int(round((lat+89.975)/.05)),int(round((lon+179.975)/.05))
        if abs(lat-(-89.975+iy*.05))>2e-5 or abs(lon-(-179.975+ix*.05))>2e-5:
            raise ValueError("GPP comparison cell does not align with the canonical 0.05-degree grid")
        key=(row["scenario"],year,iy,ix,product)
        rank=(-float(row["coverage"]),str(row["region_id"]))
        if key not in chosen or rank<chosen[key][0]:
            row.update(reference_product=product,common_cell_lat=round(-89.975+iy*.05,6),
                       common_cell_lon=round(-179.975+ix*.05,6),common_year=year,
                       model_value=float(row["model"]),reference_value=float(row["reference"]))
            chosen[key]=(rank,row)
    common={key[:-1] for key in chosen if all(key[:-1]+(p,) in chosen for p in products)}
    return [chosen[key+(product,)][1] for key in sorted(common) for product in products]


def reference_group_weights(ds, rows, qa_root, source_grid_path=None,pyrome_path=None):
    """Actual original land-class areas inside each native reference support.

    These weights describe the composition of a coarse comparison cell; they do
    NOT manufacture class-specific observations out of a mixed reference pixel.
    The model/reference pair remains the same native-support pair in every group.
    """
    from .grouping import get_groups
    region_id = str(ds.attrs.get("region_id", Path(qa_root).name))
    info = get_groups(region_id, qa_root,source_grid_path,pyrome_path)
    if not np.array_equal(ds.lat.values, info["lat"]) or not np.array_equal(ds.lon.values, info["lon"]):
        raise ValueError("Reference grouping coordinates differ from output coordinates")
    # Fractions use the archived crosswalk's own physical-cell denominator;
    # the overlap operator subsequently converts them to WGS84 target area.
    # Mixing area conventions in the fraction itself can spuriously exceed one.
    geometric_area = np.asarray(info["cell_area_m2"],float)
    sources = info["run_group_area_m2"]
    unique = {}
    for row in rows:
        key = (row["product"], row["variable"], row["reference_cell_id"], row["carbon_fraction"])
        unique.setdefault(key, row)
    weights = []
    for key,row in unique.items():
        matrix,_ = overlap_matrix(ds.lat.values,ds.lon.values,[row["reference_lat"]],[row["reference_lon"]],
                                  reference_lat_bounds=[[row["reference_lat_min"],row["reference_lat_max"]]],
                                  reference_lon_bounds=[[row["reference_lon_min"],row["reference_lon_max"]]])
        for name, group_area in sources.items():
            group_type, group = name.split(":",1)
            if group_type not in ("pyrome","vegetation","igbp","model_pft"):
                continue
            if row["variable"] == "cVegWood":
                if group_type == "pyrome":
                    group_area = sources["pyrome_vegetation:"+group+":forest"]
                elif group_type == "vegetation":
                    if group != "forest":
                        continue
                elif group_type == "igbp":
                    if int(group) not in range(1,6):
                        continue
                else:
                    # This would require a full pyrome×IGBP×parameter crosswalk;
                    # do not approximate a woodland observation by PFT relabeling.
                    continue
            fraction = np.divide(group_area,geometric_area,out=np.zeros_like(group_area),where=geometric_area>0)
            if np.max(fraction,initial=0)>1+1e-4:
                raise ValueError("Archived group area exceeds physical model cell area")
            area=float((matrix @ np.clip(fraction.ravel(),0,1))[0])
            if area <= 0:
                continue
            weights.append(dict(region_id=region_id,product=key[0],variable=key[1],reference_cell_id=key[2],
                                carbon_fraction=key[3],group_type=group_type,group=group,group_area_m2=area,
                                group_semantics="native reference-cell agreement weighted by original-class overlap area; mixed reference values are not class-specific observations"))
    return weights


def grouped_metrics_from_pairs(rows, weights):
    lookup = {}
    for row in weights:
        key = (row["product"],row["variable"],row["reference_cell_id"],row["carbon_fraction"])
        lookup.setdefault(key,[]).append(row)
    groups = {}
    for row in rows:
        for weight in lookup.get((row["product"],row["variable"],row["reference_cell_id"],row["carbon_fraction"]),[]):
            for threshold in (90,80):
                if not row["accepted_"+str(threshold)]:
                    continue
                for year in (str(row["period_start"])[:4],"all_available"):
                    key=(row["region_id"],row["scenario"],row["product"],row["variable"],row["units"],row["carbon_fraction"],
                         weight["group_type"],weight["group"],year,threshold)
                    groups.setdefault(key,[]).append((row["model"],row["reference"],weight["group_area_m2"]))
    results=[]
    for key, triplets in groups.items():
        values=np.asarray(triplets,float)
        metric=weighted_metrics(values[:,0],values[:,1],values[:,2])
        metric.update(dict(zip(("region_id","scenario","product","variable","units","carbon_fraction", "group_type","group","year","minimum_coverage_pct"),key)))
        metric["interpretation"]="Area-weighted agreement of mixed native reference cells; not resolved class-specific observations"
        results.append(metric)
    return results


def _write_csv(path, rows, fields=None):
    if fields is None:
        fields = sorted({k for row in rows for k in row})
    with open(path, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)


def _model_grid_display(ds, rows, output):
    """Coarse reference copied to fine centers for display, never for metrics."""
    groups = {}
    for row in rows:
        if np.isfinite(row["reference"]):
            key = (row["product"], row["variable"], row["scenario"], str(row["carbon_fraction"]))
            groups.setdefault(key, {}).setdefault(int(row["reference_cell_id"]), []).append(row)
    arrays = dict(lat=ds.lat.values, lon=ds.lon.values, run_valid_mask=ds.run_valid_mask.values)
    metadata = []
    yy, xx = np.meshgrid(ds.lat.values, ds.lon.values, indexing="ij")
    for number, (key, cells) in enumerate(sorted(groups.items())):
        display = np.full(yy.shape, np.nan, dtype=np.float32)
        source_ids = np.full(yy.shape, -1, dtype=np.int64)
        for cellid, records in cells.items():
            flux = records[0]["variable"] != "cVegWood"
            denominator = sum(r["days"] for r in records) if flux else len(records)
            value = sum(r["reference"] for r in records) / denominator
            record = records[0]
            belongs = ((yy >= record["reference_lat_min"]) & (yy < record["reference_lat_max"]) &
                       (xx >= record["reference_lon_min"]) & (xx < record["reference_lon_max"]) &
                       ds.run_valid_mask.values.astype(bool))
            display[belongs] = value
            source_ids[belongs] = cellid
        name = f"display_{number:02d}"
        arrays[name] = display
        arrays[name + "_source_cell_id"] = source_ids
        metadata.append(dict(array=name, product=key[0], variable=key[1], scenario=key[2], carbon_fraction=key[3],
                             interpretation="Display only: native reference pixel values copied to model centers; not independent observations",
                             units="gC m-2" if key[1] == "cVegWood" else ("mm day-1" if key[1] in ("evapotranspiration", "transpiration") else "gC m-2 day-1")))
    np.savez_compressed(output / "model_grid_reference_display.npz", **arrays)
    _atomic_json(output / "model_grid_reference_display.json", metadata)


def compare_region(summary_path, output_dir, config=None):
    """Execute supported reference comparisons, preserving product-level blockers."""
    import xarray as xr
    cfg = dict(config or {})
    # Standard deployment cache may be prepared independently on a networked
    # login host; compute workers only read its verified local files.
    default_manifest = Path(os.environ.get("SINDBAD_ANALYSIS_ROOT", "/path/to/analysis"))/"reference_cache/fluxcom_official_monthly/manifest.json"
    if "fluxcom_manifest" not in cfg and default_manifest.is_file():
        cfg["fluxcom_manifest"] = str(default_manifest)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    years = list(cfg.get("mod17_years", range(2014, 2022)))
    template = cfg.get("mod17_template", MOD17_TEMPLATE)
    sources = [_identity(template.format(year=y)) for y in years] + [_identity(cfg.get("cci_path", CCI_PATH))]
    if cfg.get("fluxcom_manifest"):
        manifest_path=Path(cfg["fluxcom_manifest"])
        sources.append(_identity(manifest_path))
        if manifest_path.is_file():
            for record in json.loads(manifest_path.read_text()).get("files",[]):
                cache_path=Path(record["path"])
                sources.append(_identity(cache_path if cache_path.is_absolute() else manifest_path.parent/cache_path))
    for key in ("source_geometry","pyrome_path"):
        if cfg.get(key):
            sources.append(_identity(cfg[key]))
    region_for_identity=Path(summary_path).parent.name
    if cfg.get("qa_root"):
        for relative in ("ownership.npz","run_masks.npz","static/static_inputs.npz"):
            sources.append(_identity(Path(cfg["qa_root"])/region_for_identity/relative))
    mod16_enabled=cfg.get("enable_mod16", bool(cfg.get("mod16_archive_root") or os.environ.get("WISP_ANALYSIS_MOD16_ARCHIVE")))
    mod16_years=list(cfg.get("mod16_years",range(2015,2022)))
    mod16_sources=[]
    if mod16_enabled:
        from .mod16 import mod16_source_identity
        with xr.open_dataset(summary_path) as identity_ds:
            mod16_sources=mod16_source_identity(identity_ds,mod16_years,cfg.get("mod16_archive_root"))
    mod16_code=Path(__file__).with_name("mod16.py")
    fingerprint = _digest(dict(schema=SCHEMA_VERSION, code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                               grouping_code_sha256=hashlib.sha256(Path(__file__).with_name("grouping.py").read_bytes()).hexdigest(),
                               mod16_code_sha256=hashlib.sha256(mod16_code.read_bytes()).hexdigest() if mod16_enabled and mod16_code.is_file() else None,
                               mod16_enabled=mod16_enabled, mod16_sources=mod16_sources,
                               summary=_identity(summary_path), config=cfg, sources=sources))
    done = output / "reference_complete.json"
    if done.exists():
        completed=json.loads(done.read_text())
        if (completed.get("fingerprint")==fingerprint and completed.get("output_files") and
                all(_identity(item["path"])==item for item in completed["output_files"])):
            return completed
    allrows, statuses = [], []
    with xr.open_dataset(summary_path) as ds:
        region_id = str(ds.attrs.get("region_id", Path(summary_path).parent.name))
        ds.attrs["region_id"] = region_id  # in-memory only; source NetCDF stays read-only
        # Compact summaries are small; load each used compressed variable once.
        # Otherwise a 230-period GPP chunk is decompressed for each single period.
        needed=("gpp_modis_sum","gpp_monthly_sum","nee_monthly_sum",
                "evapotranspiration_monthly_sum","transpiration_monthly_sum",
                "cVegWood_annual_mean","evapotranspiration__evapotranspiration_annual")
        for name in needed:
            if name in ds:
                ds[name]=ds[name].load()
        adapters = [("MOD17A2HGF.061", lambda: mod17_pairs(ds, region_id, template, years)),
                    ("ESA_CCI_Biomass_v6", lambda: cci_pairs(ds, region_id, cfg.get("cci_path", CCI_PATH)))]
        if mod16_enabled:
            from .mod16 import mod16_pairs
            mod16_cache=cfg.get("mod16_cache",str(Path(os.environ.get("SINDBAD_ANALYSIS_ROOT","/path/to/analysis"))/"reference_cache/mod16_native"))
            for et_year in mod16_years:
                adapters.append((f"MOD16A2GF.061_annual{et_year}",lambda y=et_year:mod16_pairs(ds,region_id,mod16_cache,year=y,archive_root=cfg.get("mod16_archive_root"))))
            statuses.append(dict(product="MOD16A2GF.061_2014",status="blocked",reason="Checked 2014-01-01 archives contain duplicate scientifically different processing versions; no version is selected implicitly. Audited 2015–2021 are processed separately.",affects=["evapotranspiration"]))
        else:
            statuses.append(dict(product="MOD16A2GF.061", status="blocked", reason="MOD16 archive is not configured. The adapter uses audited unique 2015–2021 granules and preserves inherited input QC; 2014 requires explicit version resolution.", affects=["evapotranspiration"]))
        if cfg.get("fluxcom_manifest"):
            adapters.append(("FLUXCOM-X_X-BASE_v1.0", lambda: fluxcom_verified_monthly_pairs(ds, region_id, cfg["fluxcom_manifest"])))
        else:
            statuses.append(dict(product="FLUXCOM-X_X-BASE_v1.0", status="blocked",
                                 reason="No verified official monthly cache yet. Standard local links are broken; old_aggs provenance/units not proven. Run explicit prepare-fluxcom network job.",
                                 official_source=FLUXCOM_ROOT, affects=["gpp", "nee", "evapotranspiration", "transpiration"]))
        statuses.extend([
            dict(product="MOD15A2H.061", status="not_applicable", reason="LAI is not saved or uniquely recoverable in this model configuration; no synthetic model LAI is created.", affects=["lai"]),
        ])
        for product, adapter in adapters:
            try:
                rows = list(adapter())
                allrows.extend(rows)
                status=dict(product=product, status="complete" if rows else "no_common_period",
                                     n_pairs=len(rows), accepted_90=sum(r["accepted_90"] for r in rows),
                                     accepted_80=sum(r["accepted_80"] for r in rows),
                                     coverage_basis="full native reference pixel area; conservative when exact land support unavailable",
                                     regional_absolute_totals="not computed; model and product area bases unresolved",
                                     source_reference_independence="Reference estimates, not ground truth; shared calibration or EO lineage is possible")
                if product=="FLUXCOM-X_X-BASE_v1.0":
                    manifest=json.loads(Path(cfg["fluxcom_manifest"]).read_text())
                    expected={(v,int(y)) for v in manifest.get("requested_variables",ICOS_COLLECTIONS) for y in manifest.get("requested_years",range(2014,2022))}
                    present={(r["variable"],int(r["request"]["year"])) for r in manifest["files"] if "request" in r}
                    missing=sorted(expected-present)
                    status["missing_variable_years"]=[list(x) for x in missing]
                    status["source_preparation_errors"]=manifest.get("errors",[])
                    if missing:
                        status["status"]="partial"
                statuses.append(status)
            except (OSError, ValueError, KeyError, ImportError, RuntimeError) as exc:
                statuses.append(dict(product=product, status="blocked", reason=f"{type(exc).__name__}: {exc}"))
        if any(_identity(s["path"])!=s for s in sources):
            raise RuntimeError("A reference source or manifest changed during comparison; retry against a frozen cache snapshot")
        if mod16_enabled and mod16_source_identity(ds,mod16_years,cfg.get("mod16_archive_root"))!=mod16_sources:
            raise RuntimeError("MOD16 source identity changed during comparison; retry against stable source granules")
        temp_pairs = output / "reference_pairs.csv.gz.partial"
        with gzip.open(temp_pairs, "wt", newline="") as stream:
            writer = csv.DictWriter(stream, PAIR_FIELDS)
            writer.writeheader()
            writer.writerows(allrows)
        os.replace(temp_pairs, output / "reference_pairs.csv.gz")
        metrics = metrics_from_pairs(allrows)
        _write_csv(output / "reference_metrics.csv", metrics)
        annual=annualize_reference_pairs(allrows)
        with gzip.open(output/"annual_reference_pairs.csv.gz","wt",newline="") as stream:
            writer=csv.DictWriter(stream,PAIR_FIELDS);writer.writeheader();writer.writerows(annual)
        _write_csv(output/"annual_reference_metrics.csv",metrics_from_pairs(annual))
        qa_root=cfg.get("qa_root",os.environ.get("SINDBAD_BATCH_QA_ROOT", "/path/to/batch/qa"))
        if (Path(qa_root)/region_id).is_dir():
            try:
                weights=reference_group_weights(ds,allrows,qa_root,cfg.get("source_geometry"),cfg.get("pyrome_path"))
                with gzip.open(output/"reference_group_weights.csv.gz","wt",newline="") as stream:
                    fields=list(weights[0]) if weights else ["region_id","product","variable","reference_cell_id","carbon_fraction","group_type","group","group_area_m2"]
                    writer=csv.DictWriter(stream,fields);writer.writeheader();writer.writerows(weights)
                grouped=grouped_metrics_from_pairs(allrows,weights)
                _write_csv(output/"reference_group_metrics.csv",grouped)
                statuses.append(dict(product="reference_groups",status="complete",n_support_weights=len(weights),n_metrics=len(grouped),
                                     interpretation="Actual original-class fractions within native reference support; never cube-dominant labels"))
            except (OSError,ValueError,KeyError) as exc:
                statuses.append(dict(product="reference_groups",status="blocked",reason=str(exc)))
        _model_grid_display(ds, allrows, output)
    _atomic_json(output / "statuses.json", statuses)
    result = dict(fingerprint=fingerprint, schema=SCHEMA_VERSION, region_id=region_id,
                  n_pairs=len(allrows), n_metrics=len(metrics), statuses=statuses, sources=sources,mod16_sources=mod16_sources,
                  output_files=[_identity(p) for p in output.iterdir() if p.is_file() and p != done],
                  completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    _atomic_json(done, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compare")
    p.add_argument("--summary", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--config")
    f = sub.add_parser("prepare-fluxcom")
    f.add_argument("--variable", required=True, choices=["GPP", "NEE", "ET", "ET_T"])
    f.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    f.add_argument("--output", required=True)
    f.add_argument("--start", default="2014-01-01")
    f.add_argument("--end", default="2022-01-01")
    official = sub.add_parser("prepare-fluxcom-monthly")
    official.add_argument("--variables", nargs="+", default=["GPP","NEE","ET","ET_T"],choices=list(ICOS_COLLECTIONS))
    official.add_argument("--years", nargs="+", type=int, default=list(range(2014,2022)))
    official.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST","SOUTH","EAST","NORTH"))
    official.add_argument("--output", required=True)
    official.add_argument("--workers",type=int,default=2)
    args = parser.parse_args()
    if args.command == "compare":
        cfg = json.loads(Path(args.config).read_text()) if args.config else None
        result = compare_region(args.summary, args.output, cfg)
    elif args.command == "prepare-fluxcom":
        result = prepare_fluxcom_monthly(args.variable, args.bbox, args.output, args.start, args.end)
    else:
        from concurrent.futures import ProcessPoolExecutor,as_completed
        import multiprocessing
        records,errors=[],[]
        # HDF5/netCDF libraries are not thread-safe in the installed stack.
        with ProcessPoolExecutor(max_workers=args.workers,mp_context=multiprocessing.get_context("spawn")) as pool:
            futures={pool.submit(prepare_fluxcom_official_monthly,v,y,args.bbox,args.output):(v,y) for v in args.variables for y in args.years}
            for future in as_completed(futures):
                variable,year=futures[future]
                try:
                    record=future.result();records.append(record)
                    print(json.dumps(dict(event="reference_cache_ready",variable=variable,year=year,path=record["path"])),flush=True)
                except Exception as exc:
                    error=dict(variable=variable,year=year,error=f"{type(exc).__name__}: {exc}")
                    errors.append(error);print(json.dumps(error),flush=True)
                manifest=dict(product="FLUXCOM-X X-BASE v1.0",complete_months_only=True,
                              requested_variables=args.variables,requested_years=args.years,
                              files=sorted(records,key=lambda r:(r["variable"],r["request"]["year"])),errors=errors,
                              license="ICOS RI, licensed under CC BY 4.0",bbox=args.bbox)
                _atomic_json(Path(args.output)/"manifest.json",manifest)
        result=manifest
        if errors:
            print(json.dumps(dict(manifest=str(Path(args.output)/"manifest.json"),n_ready=len(records),errors=errors),indent=2))
            raise SystemExit(2)
        result=dict(manifest=str(Path(args.output)/"manifest.json"),n_ready=len(records),errors=errors)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
