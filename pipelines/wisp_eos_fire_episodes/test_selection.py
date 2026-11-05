import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import selection as s


class SelectionTests(unittest.TestCase):
    def test_documented_example_and_uniform_blocks(self):
        result = s.build_region_selection("T", {"region_id": "example"}, ["2016-02-04", "2016-02-07"])
        self.assertEqual(result["selected_dates"][0], "2016-01-30T00:00:00")
        self.assertEqual(result["selected_dates"][-1], "2016-02-12T00:00:00")
        self.assertEqual(result["summary"], {"fire_days": 2, "selected_days": 14, "episodes": 1, "tasks": 2})
        self.assertEqual([len(b["dates"]) for b in result["blocks"]], [7, 7])
        self.assertEqual([len(c) for c in s.balanced_chunks(s.daily_range("2020-02-24", "2020-03-05"))], [6, 5])

    def test_boundary_clip_no_recursive_extension(self):
        selected = s.select_dates([s.START, s.END])
        self.assertEqual(len(selected), 12)
        self.assertEqual(selected[0].isoformat(), s.START)
        self.assertEqual(selected[-1].isoformat(), s.END)
        self.assertEqual([len(e) for e in s.merge_dates(selected)], [6, 6])

    def test_adjacent_merge_but_no_gap_fill(self):
        # Eleven days apart produces adjacent 11-day buffers; twelve leaves a one-day gap.
        self.assertEqual(len(s.merge_dates(s.select_dates(["2016-02-04", "2016-02-15"]))), 1)
        episodes = s.merge_dates(s.select_dates(["2016-02-04", "2016-02-16"]))
        self.assertEqual(len(episodes), 2)
        self.assertNotIn(date(2016, 2, 10), [d for e in episodes for d in e])

    def test_cross_year_and_leap_day(self):
        days = s.select_dates(["2019-12-30", "2020-02-29"])
        self.assertIn(date(2020, 1, 4), days)
        self.assertIn(date(2020, 2, 29), days)
        chunks = [c for e in s.merge_dates(days) for c in s.balanced_chunks(e)]
        self.assertEqual([d for c in chunks for d in c], days)

    def test_empty_dates_and_invalid_dates(self):
        self.assertEqual(s.select_dates([]), [])
        with self.assertRaises(ValueError):
            s.select_dates(["2016-01-01", "2016-01-01"])
        with self.assertRaises(ValueError):
            s.select_dates(["2013-12-31"])
        with self.assertRaises(ValueError):
            s.balanced_chunks(["2016-01-01", "2016-01-03"])
        with self.assertRaises(ValueError):
            s.as_date("2016-01-01T12:00:00")

    def test_manifest_hash_mapping_and_immutable_publication(self):
        region = s.build_region_selection("T", {"region_id": "example", "cube_sha256": "abc"}, ["2016-01-02"])
        manifest = s.make_manifest([region])
        self.assertTrue(s.validate_manifest(manifest))
        self.assertEqual(manifest, s.make_manifest([region]))
        changed = copy.deepcopy(manifest)
        next(iter(changed["tasks"].values()))["dates"].pop()
        with self.assertRaisesRegex(ValueError, "partition"):
            s.validate_manifest(changed)
        with tempfile.TemporaryDirectory() as folder:
            path = s.write_manifest(manifest, folder)
            self.assertEqual(json.loads(path.read_text()), manifest)
            s.write_manifest(manifest, folder)
            altered = s.make_manifest([s.build_region_selection("T", {"region_id": "example"}, ["2016-01-03"])])
            with self.assertRaisesRegex(ValueError, "overwrite"):
                s.write_manifest(altered, folder)


class CubeReaderTests(unittest.TestCase):
    def setUp(self):
        try:
            import netCDF4
            import numpy as np
        except ImportError:
            self.skipTest("netCDF4 and NumPy required for source-reader tests")
        self.nc, self.np = netCDF4, np
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cube.nc"
        with self.nc.Dataset(self.path, "w") as ds:
            for name, size in [("time", 3), ("lat", 128), ("lon", 128)]:
                ds.createDimension(name, size)
            t = ds.createVariable("time", "i8", ("time",))
            t.units = "days since 2014-01-12 00:00:00"
            t.calendar = "proleptic_gregorian"
            t[:] = [0, 1, 2]
            for name in ("lat", "lon"):
                ds.createVariable(name, "f8", (name,))[:] = self.np.arange(128) * 0.01
            fire = ds.createVariable("viirs_fire_binary", "f4", ("time", "lat", "lon"), fill_value=self.np.nan)
            fire[:] = 0
            fire[0, 16, 16] = 1
            fire[1, 15, 15] = 1  # Outside the centre: must not create an event.

    def record(self):
        stat = self.path.stat()
        return {"cube_path": str(self.path), "cube_sha256": s.file_sha256(self.path),
                "cube_stat": {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}}

    def test_only_centre_used_and_no_extra_day_shift(self):
        days, audit = s.read_cube_fire_days(self.record(), start="2014-01-12", end="2014-01-14")
        self.assertEqual(days, [date(2014, 1, 12)])
        self.assertTrue(audit["all_centre_labels_finite_binary"])

    def test_missing_label_rejected(self):
        with self.nc.Dataset(self.path, "a") as ds:
            ds["viirs_fire_binary"][1, 20, 20] = self.np.nan
        with self.assertRaisesRegex(ValueError, "Unknown VIIRS"):
            s.read_cube_fire_days(self.record(), start="2014-01-12", end="2014-01-14")

    def test_missing_or_duplicate_date_rejected(self):
        with self.nc.Dataset(self.path, "a") as ds:
            ds["time"][:] = [0, 2, 2]
        with self.assertRaisesRegex(ValueError, "date axis"):
            s.read_cube_fire_days(self.record(), start="2014-01-12", end="2014-01-14")

    def test_nonbinary_label_rejected(self):
        with self.nc.Dataset(self.path, "a") as ds:
            ds["viirs_fire_binary"][1, 20, 20] = 0.5
        with self.assertRaisesRegex(ValueError, "nonbinary"):
            s.read_cube_fire_days(self.record(), start="2014-01-12", end="2014-01-14")

    def test_changed_cube_rejected(self):
        record = self.record()
        record["cube_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum"):
            s.read_cube_fire_days(record, start="2014-01-12", end="2014-01-14")


if __name__ == "__main__":
    unittest.main()
