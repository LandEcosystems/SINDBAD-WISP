import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import sparse

from result_analysis.grouping import aggregate_groups, classify_source_cells
from result_analysis.registry import VARIABLES, components, inventory
from result_analysis.provenance import inspect_run, _decode


class GroupingTests(unittest.TestCase):
    def test_fractional_groups_and_invalid_runtime_pixels(self):
        # Pixel 0 contains 2 m2 forest and 3 m2 grass; pixel 1 is not owned.
        # Pixel 2 is water/excluded despite model outputs possibly being zero.
        w = sparse.csr_matrix([[2, 3, 0], [0, 4, 1], [5, 0, 0], [0, 0, 6]])
        groups = aggregate_groups(w, [1, 10, 17], [1, 0, 2],
                                  np.array([[1, 0], [1, 1]], bool),
                                  np.array([[1, 1], [0, 1]], bool),
                                  np.array([[5, 10], [17, 17]]))
        np.testing.assert_array_equal(groups["overall:all"], [[5, 0], [0, 0]])
        np.testing.assert_array_equal(groups["vegetation:forest"], [[2, 0], [0, 0]])
        np.testing.assert_array_equal(groups["vegetation:grass"], [[3, 0], [0, 0]])
        self.assertEqual(groups["pyrome:unknown"].sum(), 3)
        self.assertEqual(groups["model_pft:5"].sum(), 5)
        self.assertEqual(groups["igbp:1"].sum(), 2)
        self.assertEqual(sum(v.sum() for k, v in groups.items() if k.startswith("pyrome_vegetation:")), 5)

    def test_unclassified_pyrome_not_nearest_filled(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pyromes.csv"
            p.write_text("long,lat,Class5,Uncert\n0.25,40.25,1,0.2\n")
            codes, uncertainty = classify_source_cells(np.array([40.1, 40.1]), np.array([0.1, 0.6]), p)
            np.testing.assert_array_equal(codes, [1, 0])
            self.assertTrue(np.isnan(uncertainty[1]))

    def test_invalid_crosswalk_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_groups(sparse.csr_matrix([[-1]]), [1], [1], [[True]], [[True]])

    def test_saved_registry_count_and_stock_kind(self):
        self.assertEqual(len(VARIABLES), 13)
        self.assertEqual(len(components()), 50)
        self.assertEqual(VARIABLES["cEco"]["kind"], "meanstock")
        self.assertFalse(VARIABLES["nee"]["nonnegative"])

    def test_inventory_does_not_replace_a_different_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "done").mkdir()
            (p / "done/B1_example").write_text("done")
            snap = p / "analysis/snapshot.json"
            first = inventory(p, snap)
            self.assertEqual(first["n_regions"], 1)
            self.assertEqual(inventory(p, snap)["created_utc"], first["created_utc"])
            (p / "done/B1_another").write_text("done")
            with self.assertRaises(FileExistsError):
                inventory(p, snap)
            self.assertEqual(json.loads(snap.read_text())["n_regions"], 1)

    def test_actual_metadata_missing_is_failure_not_default(self):
        with tempfile.TemporaryDirectory() as d:
            status = inspect_run(d)
            self.assertEqual(status["status"], "fail")
            self.assertIn("actual_run_metadata_readable", [c["check"] for c in status["checks"]])

    def test_jld_metadata_reference_decoding(self):
        import h5py
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.h5"
            with h5py.File(p, "w") as h:
                a = h.create_dataset("scalar", data=np.array([0.99]))
                b = h.create_dataset("text", data=b"f_pft")
                self.assertEqual(_decode(h, b.ref), "f_pft")
                self.assertEqual(_decode(h, a.ref), [0.99])


if __name__ == "__main__":
    unittest.main()
