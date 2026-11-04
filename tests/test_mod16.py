import datetime as dt
import tempfile
import json
import hashlib
import unittest
from unittest.mock import patch
from pathlib import Path
import zipfile

import numpy as np

from result_analysis.mod16 import decode_et, unique_granule, annual_tile, _gdal, _write_verified_raster, _cache_lock, CACHE_PROTOCOL


class Mod16Tests(unittest.TestCase):
    def test_directory_lock_excludes_second_writer_without_stealing(self):
        with tempfile.TemporaryDirectory() as folder:
            owner = Path(folder) / "build.lockdir" / "owner.json"
            with _cache_lock(folder):
                initial = owner.read_text()
                with self.assertRaises(TimeoutError):
                    with _cache_lock(folder, timeout=0.01, poll=0.005):
                        self.fail("Second writer entered the protected section")
                self.assertEqual(owner.read_text(), initial)
            self.assertFalse(owner.parent.exists())
            with _cache_lock(folder):
                self.assertNotEqual(owner.read_text(), initial)

    def test_directory_lock_releases_after_builder_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "builder failed"):
                with _cache_lock(folder):
                    raise RuntimeError("builder failed")
            self.assertFalse((Path(folder) / "build.lockdir").exists())

    def test_scale_and_missing_are_not_zeros(self):
        raw = np.array([0, 100, -10, 32700, 32761, 32767], np.int16)
        values, strict = decode_et(raw, np.zeros(raw.shape, np.uint8))
        np.testing.assert_allclose(values[:4], [0, 10, -1, 3270])
        self.assertTrue(np.isnan(values[4:]).all())
        np.testing.assert_array_equal(strict, [True, True, True, True, False, False])

    def test_gapfilled_primary_does_not_silently_use_strict_qc(self):
        # Gapfilled ET stays available while the original cloud-contaminated
        # LAI/FPAR input QC is retained as a separate diagnostic.
        values, strict = decode_et(np.array([100, 100]), np.array([0, 8]))
        np.testing.assert_allclose(values, [10, 10])
        np.testing.assert_array_equal(strict, [True, False])

    def test_multiple_versions_are_not_implicitly_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            names = ["2021.01.01/MOD16A2GF.A2021001.h19v04.061.2022014000000.hdf",
                     "2021.01.01/MOD16A2GF.A2021001.h19v04.061.2022015000000.hdf"]
            with zipfile.ZipFile(root / "c2021.01.01.zip", "w") as z:
                for name in names:
                    z.writestr(name, b"example")
            with self.assertRaises(ValueError):
                unique_granule(dt.date(2021, 1, 1), "h19v04", root)

    def test_unverified_earlier_year_refused(self):
        with self.assertRaises(ValueError):
            annual_tile(2014, "h19v04", "/unused")

    def test_annual_calendar_completeness_and_scaling(self):
        gdal = _gdal()
        from osgeo import osr
        reference = osr.SpatialReference(); reference.ImportFromEPSG(4326)
        geometry = ((0, .01, 0, 1, 0, -.01), reference.ExportToWkt(), (2, 2))

        def identify(day, tile, root):
            return {"day": str(day), "tile": tile}

        def read(record, temporary):
            day = dt.date.fromisoformat(record["day"])
            last = min(day + dt.timedelta(days=7), dt.date(day.year, 12, 31))
            water = np.full((2, 2), 2.0 * ((last - day).days + 1))
            if day.day == 1 and day.month == 1:
                water[0, 1] = np.nan
            return (water, np.isfinite(water), np.zeros((2, 2), np.uint8), geometry,
                    {"RANGEBEGINNINGDATE": str(day), "RANGEENDINGDATE": str(last)})

        with tempfile.TemporaryDirectory() as directory:
            with patch("result_analysis.mod16.unique_granule", side_effect=identify), patch("result_analysis.mod16._read_granule", side_effect=read):
                for year, expected in [(2015, 730.), (2020, 732.)]:
                    file = annual_tile(year, "h19v04", directory)
                    source = gdal.Open(str(file)); values = source.ReadAsArray(); source = None
                    self.assertEqual(values[0, 0, 0], expected)
                    self.assertTrue(np.isnan(values[0, 0, 1]))
                    self.assertEqual(values[1, 0, 1], 0)
                    status = json.loads(file.with_name("complete.json").read_text())
                    self.assertEqual(status["sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
                    self.assertEqual(status["cache_protocol"], CACHE_PROTOCOL)
                    self.assertTrue(status["full_readback_verified"])
                    with patch("result_analysis.mod16._read_granule", side_effect=AssertionError("Unexpected rebuild")):
                        self.assertEqual(annual_tile(year, "h19v04", directory), file)

    def test_compressed_multiband_writer_is_closed_and_decodes_before_return(self):
        gdal = _gdal()
        from osgeo import osr
        reference = osr.SpatialReference(); reference.ImportFromEPSG(4326)
        geometry = ((0, .01, 0, 1, 0, -.01), reference.ExportToWkt(), (512, 512))
        rng = np.random.default_rng(42)
        bands = [rng.normal(size=geometry[2]) for _ in range(4)]
        bands[0][0, 0] = np.nan
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "staging.tif"
            digest = _write_verified_raster(path, geometry, bands, ["a", "b", "c", "d"], {"test": "yes"})
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            published = path.with_name("published.tif"); path.replace(published)
            ds = gdal.Open(str(published)); actual = ds.ReadAsArray(); ds = None
            np.testing.assert_array_equal(actual, np.asarray(bands, np.float32))
            self.assertEqual(digest, hashlib.sha256(published.read_bytes()).hexdigest())

    def test_failed_readback_cannot_publish_completion(self):
        from osgeo import osr
        reference = osr.SpatialReference(); reference.ImportFromEPSG(4326)
        geometry = ((0, .01, 0, 1, 0, -.01), reference.ExportToWkt(), (2, 2))
        def identify(day, tile, root):
            return {"day": str(day), "tile": tile}
        def read(record, temporary):
            day = dt.date.fromisoformat(record["day"])
            return (np.ones((2, 2)), np.ones((2, 2), bool), np.zeros((2, 2), np.uint8), geometry,
                    {"RANGEBEGINNINGDATE": str(day), "RANGEENDINGDATE": str(min(day+dt.timedelta(days=7),dt.date(2015,12,31)))})
        with tempfile.TemporaryDirectory() as folder:
            with patch("result_analysis.mod16.unique_granule", side_effect=identify), \
                 patch("result_analysis.mod16._read_granule", side_effect=read), \
                 patch("result_analysis.mod16._write_verified_raster", side_effect=RuntimeError("decode error")):
                with self.assertRaisesRegex(RuntimeError, "decode error"):
                    annual_tile(2015, "h19v04", folder)
            self.assertFalse(list(Path(folder).rglob("complete.json")))
            self.assertFalse(list(Path(folder).rglob("annual_et.tif")))


if __name__ == "__main__":
    unittest.main()
