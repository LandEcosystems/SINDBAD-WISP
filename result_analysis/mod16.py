"""Audited MOD16A2GF C6.1 annual ET, using uniquely identified local granules.

The comparison uses the audited 2015-2021 years. The 2014-01-01 archive
contains multiple scientifically different C6.1 reprocessings per date/tile;
this reader never resolves that ambiguity by choosing the newest filename.
The checked Mediterranean 2015-2021 granules were unique; every used tile
and date is rechecked at runtime. Missing or ambiguous years remain explicit.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

import numpy as np

from .io import atomic_json, canonical_hash, file_hash
from .registry import file_identity

ARCHIVE_ROOT = Path(os.environ.get("WISP_ANALYSIS_MOD16_ARCHIVE", "configured_sources/MOD16A2GF.061/orgdata"))
GUIDE = "https://www.earthdata.nasa.gov/s3fs-public/2025-04/MOD16_User_Guide_V61.pdf"
SPECIFICATION = "https://ladsweb.modaps.eosdis.nasa.gov/filespec/MODIS/61/MOD16A2GF_c61"
POLICY = "valid-range 46-composite annual total; preserve input QC diagnostics; no implicit latest-version selection"
CACHE_PROTOCOL = "closed-gdal-handles-full-readback-v2"


def _gdal():
    """Use a functioning, recorded PROJ database in relocatable environments."""
    from osgeo import gdal, osr
    from pyproj.datadir import get_data_dir
    database = Path(os.environ.get("WISP_ANALYSIS_PROJ_DATA", get_data_dir()))
    if not (database / "proj.db").is_file():
        raise FileNotFoundError("PROJ database missing from configured reference environment")
    osr.SetPROJSearchPaths([str(database)])
    gdal.UseExceptions()
    return gdal


def decode_et(raw, qc, scale=0.1, offset=0.0):
    """Return physical 8-day ET and an optional strict-input-QC diagnostic.

    Primary GF validity follows the product's encoded valid range. The user
    guide permits this because poor input LAI/FPAR has already been gap-filled.
    The inherited QC describes that input, not an independent ET error bound.
    Valid negative values are retained; missing and non-terrestrial codes are
    not zero ET. One kg water per square metre equals one millimetre.
    """
    raw, qc = np.asarray(raw), np.asarray(qc, dtype=np.uint8)
    if raw.shape != qc.shape or abs(scale - 0.1) > 1e-12 or offset != 0:
        raise ValueError("Unexpected MOD16 shape or physical scaling")
    valid = (raw >= -32767) & (raw <= 32700)
    et = np.where(valid, (raw.astype(np.float64) - offset) * scale, np.nan)
    cloud = (qc >> 3) & 3
    strict = valid & (qc != 255) & ((qc & 1) == 0) & ((qc & 4) == 0) & (((qc >> 5) & 7) <= 1) & np.isin(cloud, [0, 3])
    return et, strict


def unique_granule(day, tile, archive_root=ARCHIVE_ROOT):
    """Identify one source granule; zero or multiple candidates are explicit errors."""
    root = Path(archive_root)
    label = day.strftime("%Y.%m.%d")
    pattern = "MOD16A2GF.A" + day.strftime("%Y%j") + "." + tile + ".061.*.hdf"
    direct = sorted((root / label).glob(pattern))
    if len(direct) > 1:
        raise ValueError("Ambiguous MOD16 processing versions: " + label + "/" + tile)
    archive = root / ("c" + label + ".zip")
    # Inspect zip identity even when expanded files exist: an expanded choice
    # must not hide additional versions in the recorded source archive.
    members = []
    if archive.is_file():
        with zipfile.ZipFile(archive) as z:
            prefix = pattern.split("*")[0]
            members = [x for x in z.namelist() if Path(x).name.startswith(prefix) and x.endswith(".hdf")]
    if len(members) > 1:
        raise ValueError("Ambiguous MOD16 processing versions in " + str(archive) + ": " + str(members))
    if direct:
        if members and direct[0].name != Path(members[0]).name:
            raise ValueError("Expanded MOD16 file disagrees with unique archived version")
        return {"kind": "expanded", "file": str(direct[0]), "identity": file_identity(direct[0]), "granule": direct[0].name}
    if len(members) == 1:
        return {"kind": "zip_member", "file": str(archive), "member": members[0],
                "identity": file_identity(archive), "granule": Path(members[0]).name}
    raise FileNotFoundError("No MOD16 granule for " + label + "/" + tile)


def _read_granule(record, temporary):
    gdal = _gdal()
    if record["kind"] == "expanded":
        path = record["file"]
    else:
        path = str(Path(temporary) / record["granule"])
        with zipfile.ZipFile(record["file"]) as z:
            Path(path).write_bytes(z.read(record["member"]))
    dataset = gdal.Open(path)
    layers = {name.rsplit(":", 1)[-1]: name for name, _ in dataset.GetSubDatasets()}
    et = gdal.Open(layers["ET_500m"])
    qc = gdal.Open(layers["ET_QC_500m"])
    meta = et.GetMetadata()
    if meta.get("SHORTNAME") != "MOD16A2GF" or meta.get("VERSIONID") not in ("61", "061"):
        raise ValueError("Unverified MOD16 product identity")
    if meta.get("LOCALGRANULEID") != record["granule"] or meta.get("units") != "kg/m^2/8day":
        raise ValueError("MOD16 granule identity or units differ from contract")
    raw, quality = et.ReadAsArray(), qc.ReadAsArray()
    physical, strict = decode_et(raw, quality, float(meta["scale_factor"]), float(meta["add_offset"]))
    evidence = {k: meta.get(k) for k in ["LOCALGRANULEID", "PGEVERSION", "PRODUCTIONDATETIME", "INPUTPOINTER", "RANGEBEGINNINGDATE", "RANGEENDINGDATE", "scale_factor", "units", "valid_range"]}
    geometry = (et.GetGeoTransform(), et.GetProjection(), raw.shape)
    digest = hashlib.sha256(raw.tobytes() + quality.tobytes()).hexdigest()
    # Close HDF handles before deleting only this function's extracted member.
    et = qc = dataset = None
    if record["kind"] == "zip_member":
        Path(path).unlink()
    return physical, strict, quality, geometry, dict(evidence, et_qc_array_sha256=digest)


def _write_verified_raster(path, geometry, bands, names, metadata):
    """Close every GDAL handle and verify lossless readback before publication."""
    gdal = _gdal()
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), geometry[2][1], geometry[2][0], len(bands), gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"])
    if ds is None:
        raise RuntimeError("Unable to create MOD16 staging raster")
    band = None
    try:
        ds.SetGeoTransform(geometry[0]); ds.SetProjection(geometry[1])
        for number, (values, name) in enumerate(zip(bands, names), 1):
            band = ds.GetRasterBand(number)
            band.WriteArray(values); band.SetDescription(name)
            band.SetNoDataValue(float("nan"))
            band = None
        ds.SetMetadata(metadata)
        ds.FlushCache()
    finally:
        # A retained Band keeps its Dataset alive. Releasing only ds can leave
        # the compressed file open after rename/checksum/publication.
        band = None
        ds = None
    check = gdal.Open(str(path), gdal.GA_ReadOnly)
    if check is None:
        raise RuntimeError("Unable to reopen MOD16 staging raster")
    try:
        if (check.RasterCount != len(bands) or
                (check.RasterYSize, check.RasterXSize) != geometry[2] or
                check.GetGeoTransform() != geometry[0]):
            raise RuntimeError("MOD16 staging raster geometry mismatch")
        for number, expected in enumerate(bands, 1):
            band = check.GetRasterBand(number)
            actual = band.ReadAsArray()
            band = None
            if actual is None or not np.array_equal(actual, np.asarray(expected, np.float32), equal_nan=True):
                raise RuntimeError("MOD16 staging raster failed complete lossless readback")
    finally:
        band = None
        check = None
    return file_hash(path)


def annual_tile(year, tile, cache_root, archive_root=ARCHIVE_ROOT):
    """Stream 46 unique composites, cache only a compact annual tile and evidence."""
    gdal = _gdal()
    if year not in range(2015, 2022):
        raise ValueError("MOD16 comparison supports audited 2015-2021; 2014 has ambiguous processing versions")
    begin, end = dt.date(year, 1, 1), dt.date(year + 1, 1, 1)
    days = [begin + dt.timedelta(days=x) for x in range(0, (end - begin).days, 8)]
    records = [unique_granule(day, tile, archive_root) for day in days]
    fingerprint = canonical_hash({"year": year, "tile": tile, "sources": records,
                                  "policy": POLICY, "cache_protocol": CACHE_PROTOCOL})
    folder = Path(cache_root) / str(year) / tile / fingerprint
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        output, done = folder / "annual_et.tif", folder / "complete.json"
        if done.exists():
            status = json.loads(done.read_text())
            if output.is_file() and status["sha256"] == file_hash(output):
                return output
            raise RuntimeError("MOD16 annual cache is missing or has changed")
        total = count = strict_count = qc_missing_count = geometry = None
        evidence = []
        with tempfile.TemporaryDirectory(prefix="members_", dir=folder) as temporary:
            for day, record in zip(days, records):
                et, strict, qc, geom, item = _read_granule(record, temporary)
                if item["RANGEBEGINNINGDATE"] != str(day) or item["RANGEENDINGDATE"] != str(min(day + dt.timedelta(days=7), end - dt.timedelta(days=1))):
                    raise ValueError("MOD16 composite date interval differs from actual calendar")
                if geometry is None:
                    geometry = geom
                    total = np.zeros(et.shape, np.float64)
                    count = np.zeros(et.shape, np.uint8)
                    strict_count = np.zeros(et.shape, np.uint8)
                    qc_missing_count = np.zeros(et.shape, np.uint8)
                elif geom != geometry:
                    raise ValueError("MOD16 native tile geometry changes across composites")
                finite = np.isfinite(et)
                total += np.where(finite, et, 0)
                count += finite.astype(np.uint8)
                strict_count += strict.astype(np.uint8)
                qc_missing_count += (qc == 255).astype(np.uint8)
                evidence.append(item)
        valid = count == len(days)
        temp = output.with_name("annual_et.partial.tif")
        bands = [np.where(valid, total, np.nan), valid.astype(float), strict_count / len(days), qc_missing_count / len(days)]
        names = ["Annual ET (mm), all 46 composites required", "Complete-year ET area validity", "Fraction with strict input QC", "Fraction with missing inherited input QC"]
        digest = _write_verified_raster(temp, geometry, bands, names,
            {"product": "MOD16A2GF.061", "year": str(year), "policy": POLICY,
             "source_fingerprint": fingerprint, "cache_protocol": CACHE_PROTOCOL})
        os.replace(temp, output)
        atomic_json(done, {"source_fingerprint": fingerprint, "sha256": digest, "sources": records,
                           "cache_protocol": CACHE_PROTOCOL, "full_readback_verified": True,
                           "granule_metadata": evidence, "composites": len(days), "calendar_days": (end - begin).days,
                           "valid_native_pixels": int(valid.sum()), "guide": GUIDE, "specification": SPECIFICATION,
                           "qc_policy": POLICY, "units": "mm year-1", "gdal_version": gdal.VersionInfo()})
        return output


def _tiles_for_bounds(west, south, east, north):
    from pyproj import Transformer
    transform = Transformer.from_crs("EPSG:4326", "+proj=sinu +R=6371007.181 +nadgrids=@null +wktext", always_xy=True)
    x, y = transform.transform([west, east, west, east], [south, south, north, north])
    width = 1111950.5196666666
    hs = np.floor((np.asarray(x) + 20015109.354) / width).astype(int)
    vs = np.floor((10007554.677 - np.asarray(y)) / width).astype(int)
    return [f"h{h:02d}v{v:02d}" for h in range(hs.min(), hs.max() + 1) for v in range(vs.min(), vs.max() + 1)]


def mod16_source_identity(ds, years, archive_root=None):
    """Enumerate actual used granules before any comparison-resume shortcut.

    Per-year failures are retained so one missing year cannot hide the other
    reference products. Archive member identities are bound to the archive's
    current size/mtime; expanded granules carry their own file identities.
    """
    from .references import cell_bounds
    source_root = ARCHIVE_ROOT if archive_root is None else Path(archive_root)
    y, x = cell_bounds(ds.lat.values), cell_bounds(ds.lon.values)
    west, east = np.floor(x.min() / .05) * .05, np.ceil(x.max() / .05) * .05
    south, north = np.floor(y.min() / .05) * .05, np.ceil(y.max() / .05) * .05
    tiles = _tiles_for_bounds(west, south, east, north)
    result = {"schema": 1, "product": "MOD16A2GF.061", "years": {}}
    for year in years:
        entry = {"status": "ready", "tiles": {}}
        try:
            if year not in range(2015, 2022):
                raise ValueError("Year outside audited 2015-2021; 2014 has ambiguous processing versions")
            begin, end = dt.date(year, 1, 1), dt.date(year + 1, 1, 1)
            days = [begin + dt.timedelta(days=x) for x in range(0, (end - begin).days, 8)]
            for tile in tiles:
                entry["tiles"][tile] = [unique_granule(day, tile, source_root) for day in days]
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            entry.update(status="blocked", reason=str(exc))
        result["years"][str(year)] = entry
    return result


def mod16_pairs(ds, region_id, cache_root, year=2021, archive_root=None):
    """Annual ET comparison on an explicit 0.05-degree processed reference grid."""
    gdal = _gdal()
    from .references import cell_bounds, _component, _matrix, _pairs_for_period
    lat_bounds, lon_bounds = cell_bounds(ds.lat.values), cell_bounds(ds.lon.values)
    south, north = np.floor(lat_bounds.min() / .05) * .05, np.ceil(lat_bounds.max() / .05) * .05
    west, east = np.floor(lon_bounds.min() / .05) * .05, np.ceil(lon_bounds.max() / .05) * .05
    ny, nx = int(round((north - south) / .05)), int(round((east - west) / .05))
    lat, lon = north - (np.arange(ny) + .5) * .05, west + (np.arange(nx) + .5) * .05
    source_root = ARCHIVE_ROOT if archive_root is None else Path(archive_root)
    files = [str(annual_tile(year, tile, cache_root, source_root)) for tile in _tiles_for_bounds(west, south, east, north)]
    virtual = gdal.BuildVRT("", files)
    if virtual is None:
        raise RuntimeError("MOD16 verified rasters could not be opened as a mosaic")
    warped = gdal.Warp("", virtual, format="MEM", dstSRS="EPSG:4326", outputBounds=[west, south, east, north],
                       width=nx, height=ny, resampleAlg="average", srcNodata=float("nan"), dstNodata=float("nan"))
    if warped is None:
        raise RuntimeError("MOD16 mosaic could not be regridded")
    values = warped.ReadAsArray()
    if values is None:
        raise RuntimeError("MOD16 regridded raster could not be decoded")
    warped = virtual = None
    # Require nearly complete native ET support independently of model support.
    reference = np.where(values[1] >= .9 - 1e-10, values[0], np.nan)
    patch = {"lat": lat, "lon": lon, "lat_bounds": cell_bounds(lat), "lon_bounds": cell_bounds(lon),
             "row_offset": int(round((90 - north) / .05)), "col_offset": int(round((west + 180) / .05)),
             "total_columns": 7200}
    matrix, area = _matrix(ds, patch)
    year_indices = np.flatnonzero(np.asarray(ds.year.values, int) == year)
    if len(year_indices) != 1:
        raise ValueError("Missing complete model year for MOD16 comparison")
    model = _component(ds, "evapotranspiration__evapotranspiration_annual", int(year_indices[0]), "year")
    begin, end = np.datetime64(f"{year}-01-01"), np.datetime64(f"{year+1}-01-01")
    for row in _pairs_for_period(region_id, ds.scenario.values, "MOD16A2GF.061", "evapotranspiration",
                                 begin, end, model, reference, patch, matrix, area, ds.run_valid_mask.values, None, "mm year-1"):
        row["support_scale"] = f"0.05-degree aggregate of native 500m MOD16; full-year {year}"
        yield row
