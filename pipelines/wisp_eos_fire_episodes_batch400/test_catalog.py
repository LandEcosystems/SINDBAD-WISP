"""Tests for region identity, event-only partitioning, and immutable enrollment."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import catalog


def selection_for(fires, token="T000177", start="2014-01-12", end="2023-12-31"):
    record = {"region_id": "test_region_" + token, "token": token,
              "cube_path": "/readonly/cube.nc", "cube_sha256": "a" * 64}
    region = catalog.selection.build_region_selection(token, record, fires, start=start, end=end)
    return catalog.selection.make_manifest([region], start=start, end=end)


class CatalogTests(unittest.TestCase):
    def test_stable_token_uses_production_tile_not_anchor(self):
        self.assertEqual(catalog.token_for({"tile_id": 830, "anchor_id": "T001755"}), "T000830")
        for value in [True, -1, 1000000, "830"]:
            with self.assertRaises(ValueError):
                catalog.token_for({"tile_id": value})

    def test_full_period_partition_exact_once_and_bounded(self):
        all_dates = catalog.selection.daily_range("2014-01-12", "2023-12-31")
        manifest = selection_for([day.isoformat() for day in all_dates])
        waves = catalog.partition_waves(manifest["tasks"])
        self.assertEqual(sum(w["days"] for w in waves), 3641)
        self.assertEqual(len(waves), 4)
        keys = [key for wave in waves for key in wave["keys"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(manifest["tasks"]))
        for wave in waves:
            self.assertLessEqual(wave["days"], 1000)
            self.assertLessEqual(wave["reserved_bytes"], 240 * catalog.GIB)
            self.assertEqual(wave["reserved_bytes"], catalog.FIXED_CACHE_BYTES + sum(
                catalog.task_reservation(manifest["tasks"][key]) for key in wave["keys"]))

    def test_capacity_enforces_whole_tasks_without_skipping_dates(self):
        manifest = selection_for(["2015-07-15", "2015-07-18"])
        tasks = manifest["tasks"]
        one = next(iter(tasks.values()))
        capacity = catalog.FIXED_CACHE_BYTES + catalog.task_reservation(one)
        waves = catalog.partition_waves(tasks, max_reserved_bytes=capacity)
        self.assertEqual(len(waves), len(tasks))
        with self.assertRaisesRegex(ValueError, "whole short task"):
            catalog.partition_waves(tasks, max_reserved_bytes=capacity - 1)

    def test_rejects_overlapping_tasks_and_mixed_regions(self):
        tasks = selection_for(["2015-01-20"])["tasks"]
        bad = copy.deepcopy(tasks)
        bad["duplicate"] = copy.deepcopy(next(iter(tasks.values())))
        with self.assertRaisesRegex(ValueError, "Duplicate selected day"):
            catalog.partition_waves(bad)
        bad = copy.deepcopy(tasks)
        list(bad.values())[-1]["region"] = "T000178"
        with self.assertRaisesRegex(ValueError, "exactly one region"):
            catalog.partition_waves(bad)

    def test_no_fire_region_is_explicit_and_has_no_tasks_or_waves(self):
        bundle = catalog.bundle_from_manifest(selection_for([]))
        self.assertEqual(bundle["status"], "no_observed_fire")
        self.assertEqual(bundle["selected_days"], 0)
        self.assertEqual(bundle["waves"], [])
        self.assertEqual(bundle["state"]["tasks"], {})
        self.assertEqual(bundle["parity_dates"], {"T000177": []})

    def test_parity_includes_distinct_block_boundary(self):
        bundle = catalog.bundle_from_manifest(selection_for(["2016-02-04", "2016-02-07"]))
        own = sorted(bundle["selection"]["tasks"].values(), key=lambda t: t["dates"][0])
        self.assertEqual(bundle["parity_dates"]["T000177"], [own[0]["dates"][0], own[1]["dates"][0]])
        singleton = catalog.bundle_from_manifest(selection_for(["2014-01-12"], start="2014-01-12", end="2014-01-12"))
        self.assertEqual(singleton["parity_dates"]["T000177"], ["2014-01-12T00:00:00"])

    def test_ready_order_is_deterministic(self):
        bundles = [catalog.bundle_from_manifest(selection_for(["2015-01-20"], token="T000002")),
                   catalog.bundle_from_manifest(selection_for([], token="T000001")),
                   catalog.bundle_from_manifest(selection_for(["2015-01-20"], token="T000003"))]
        self.assertEqual([b["token"] for b in catalog.ready_order(bundles)], ["T000002", "T000003", "T000001"])

    def test_freeze_is_idempotent_and_never_resets_progress(self):
        bundle = catalog.bundle_from_manifest(selection_for(["2015-01-20"]))
        with tempfile.TemporaryDirectory() as tmp:
            campaign = Path(tmp) / "new_campaign"
            root = campaign / "regions" / bundle["token"]
            paths = catalog.freeze_region(bundle, root, campaign)
            queue_path = Path(paths["queue_path"])
            queue = json.loads(queue_path.read_text())
            first = next(iter(queue["tasks"]))
            queue["tasks"][first]["input_status"] = "ready"
            queue_path.write_text(json.dumps(queue))
            catalog.freeze_region(bundle, root, campaign)
            self.assertEqual(json.loads(queue_path.read_text())["tasks"][first]["input_status"], "ready")
            changed = catalog.bundle_from_manifest(selection_for(["2015-01-21"]))
            with self.assertRaises(ValueError):
                catalog.freeze_region(changed, root, campaign)

    def test_future_source_change_cannot_silently_reuse_selection(self):
        first = selection_for(["2015-01-20"])
        source_changed = copy.deepcopy(first["regions"]["T000177"]["cube_record"])
        source_changed["cube_sha256"] = "b" * 64
        rebuilt = catalog.selection.make_manifest([catalog.selection.build_region_selection(
            "T000177", source_changed, ["2015-01-20"])])
        self.assertNotEqual(first["selection_sha256"], rebuilt["selection_sha256"])
        self.assertEqual(first["regions"]["T000177"]["selected_dates"],
                         rebuilt["regions"]["T000177"]["selected_dates"])
        # Unrelated future evaluation output is not accepted by this API at all.
        self.assertEqual(catalog.bundle_from_manifest(first), catalog.bundle_from_manifest(copy.deepcopy(first)))

    def test_freeze_rejects_legacy_and_outside_paths(self):
        bundle = catalog.bundle_from_manifest(selection_for([]))
        with tempfile.TemporaryDirectory() as tmp:
            campaign = Path(tmp) / "wisp_eos_fire_episodes_v1"
            with self.assertRaisesRegex(ValueError, "legacy"):
                catalog.freeze_region(bundle, campaign / "regions" / bundle["token"], campaign)
            campaign = Path(tmp) / "new"
            with self.assertRaisesRegex(ValueError, "Region output"):
                catalog.freeze_region(bundle, Path(tmp) / "old", campaign)

    def test_inventory_tracks_pending_invalid_without_changing_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regions = [{"tile_id": i, "region_id": f"r{i}"} for i in range(4)]
            path = root / "catalog.json"
            path.write_text(json.dumps({"regions": regions}))
            acceptance = root / "acceptance"
            acceptance.mkdir()
            cube = root / "cube.nc"
            cube.write_bytes(b"not-reading-NetCDF-during-lightweight-inventory")
            valid = {"passed": True, "errors": [], "region_id": "r0", "bytes": cube.stat().st_size,
                     "path": str(cube), "file_sha256": catalog.selection.file_sha256(cube),
                     "masks_path": "/readonly/masks", "owner_path": "/readonly/owner",
                     "masks_sha256": "a" * 64, "owner_sha256": "b" * 64}
            (acceptance / "r0.json").write_text(json.dumps(valid))
            (acceptance / "r1.json").write_text(json.dumps({"passed": False, "errors": ["building"]}))
            malformed = dict(valid, region_id="r3", bytes=1)
            (acceptance / "r3.json").write_text(json.dumps(malformed))
            result = catalog.inventory(path, acceptance, expected_count=4)
            self.assertEqual(result["counts"], {"accepted": 1, "pending": 2, "invalid": 1})
            self.assertEqual(result["accepted"][0]["token"], "T000000")
            self.assertEqual(sum(result["counts"].values()), 4)
            with self.assertRaisesRegex(ValueError, "exactly 400"):
                catalog.inventory(path, acceptance)
            regions[-1]["tile_id"] = 0
            path.write_text(json.dumps({"regions": regions}))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                catalog.inventory(path, acceptance, expected_count=4)

    def test_enrollment_checks_real_header_and_source_qa_identity(self):
        import numpy as np
        import netCDF4

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lat = np.linspace(35, 35.4, 128)
            lon = np.linspace(10, 10.4, 128)
            cube = root / "cube.nc"
            with netCDF4.Dataset(cube, "w") as ds:
                for name, axis in (("lat", lat), ("lon", lon)):
                    ds.createDimension(name, 128)
                    ds.createVariable(name, "f8", (name,))[:] = axis
            masks, owner = root / "masks.npz", root / "owner.npz"
            np.savez(masks, run_valid_mask=np.ones((128, 128), dtype=bool),
                     water_mask=np.zeros((128, 128), dtype=bool), cell_area_m2=np.ones((128, 128)))
            np.savez(owner, owned_mask=np.ones((128, 128), dtype=bool),
                     owned_natural_area_m2=np.ones((128, 128)))
            receipt = {"passed": True, "errors": [], "path": str(cube), "bytes": cube.stat().st_size,
                       "file_sha256": catalog.selection.file_sha256(cube), "masks_path": str(masks),
                       "masks_sha256": catalog.selection.file_sha256(masks), "owner_path": str(owner),
                       "owner_sha256": catalog.selection.file_sha256(owner)}
            ap = root / "acceptance.json"
            ap.write_text(json.dumps(receipt))
            item = {"token": "T000830", "region": {"tile_id": 830, "anchor_id": "T001755",
                    "region_id": "test", "lat": lat.tolist(), "lon": lon.tolist()},
                    "acceptance": receipt, "acceptance_path": str(ap),
                    "acceptance_sha256": catalog.selection.file_sha256(ap)}
            region_root = root / "new" / "regions" / "T000830"
            record = catalog.accepted_cube_record(item, region_root)
            self.assertEqual(record["token"], "T000830")
            self.assertTrue(record["enrollment_cube_checksum_verified"])
            self.assertFalse(region_root.exists())
            arrays = catalog.coverage_arrays(record)
            self.assertEqual(set(arrays), {"run_valid_mask", "water_mask", "cell_area_m2", "owned_mask", "owned_natural_area_m2"})
            self.assertTrue(all(array.shape == (96, 96) for array in arrays.values()))
            np.savez(masks, replaced=np.ones((128, 128)))
            with self.assertRaisesRegex(ValueError, "QA changed"):
                catalog.coverage_arrays(record)
            with self.assertRaisesRegex(ValueError, "coverage QA changed"):
                catalog.accepted_cube_record(item, region_root)
            ap.write_text(json.dumps(dict(receipt, passed=False)))
            with self.assertRaisesRegex(ValueError, "Acceptance changed"):
                catalog.accepted_cube_record(item, region_root)


if __name__ == "__main__":
    unittest.main()
