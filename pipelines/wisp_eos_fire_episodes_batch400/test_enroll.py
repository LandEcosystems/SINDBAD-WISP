"""Operational integration tests: enrollment receipts and scanner recovery."""
import copy
import errno
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

import catalog
import common
import enroll


class EnrollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "new_campaign"
        common.atomic(self.root / "config/campaign.json", {
            "scratch": str(Path(self.tmp.name) / "scratch" / "new_campaign"),
            "catalog": "/read-only/catalog.json", "acceptance": "/read-only/acceptance",
            "pilot_root": "/read-only/pilot"})
        common.atomic(self.root / "config/frozen_deployment.json", {
            "checkpoint": "/read-only/model.pt", "checkpoint_sha256": "model-sha",
            "code_commit": "frozen-model-commit", "scratch_min_free_bytes": 0})
        common.atomic(self.root / "manifests/deployment.json", {"fingerprint": "scientific-identity"})
        common.atomic(self.root / "manifests/source_files.json", {"read-only": "source-identity"})
        common.atomic(self.root / "code/release.json", {"release": "frozen-code"})

    def make_bundle(self, token, fires):
        rr = self.root / "regions" / token
        record = {"token": token, "region_id": "region_" + token, "cube_sha256": "a" * 64,
                  "cube_path": "/read-only/cube.nc", "lat": np.linspace(35, 35.4, 128).tolist(),
                  "lon": np.linspace(10, 10.4, 128).tolist(), "source_qa": {},
                  "coverage_qa": str(rr / "qa" / token / "centre_coverage.npz")}
        region = catalog.selection.build_region_selection(token, record, fires,
            audit={"date_count": 3641, "all_centre_labels_finite_binary": True,
                   "cube_sha256": record["cube_sha256"]})
        return catalog.bundle_from_manifest(catalog.selection.make_manifest([region]))

    def test_enroll_freezes_config_identity_and_does_not_prepare_scratch(self):
        token = "T000177"
        bundle = self.make_bundle(token, ["2016-02-04"])
        seen = []
        def activate(c, root):
            rr = Path(c["root"])
            identity = common.read(rr / "manifests/identity.json")
            self.assertEqual(identity["waves_sha256"], common.sha(rr / "manifests/waves.json"))
            self.assertEqual(identity["coverage_qa_sha256"], common.sha(bundle["record"]["coverage_qa"]))
            self.assertEqual(c["scratch_budget_bytes"], 256 * common.GIB)
            self.assertFalse(Path(c["scratch"]).exists())
            seen.append(c["root"])
        products = types.SimpleNamespace(verify_reuse=mock.Mock(), empty_result=mock.Mock())
        with mock.patch.object(catalog, "accepted_cube_record", return_value=bundle["record"]), \
             mock.patch.object(catalog, "prepare_region", return_value=bundle), \
             mock.patch.object(catalog, "coverage_arrays", return_value={"run_valid_mask": np.ones((96, 96), dtype=bool)}), \
             mock.patch.dict(sys.modules, {"adapter": types.SimpleNamespace(activate=activate), "batch_products": products}):
            result = enroll.enroll_one(self.root, {"token": token})
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["selected_days"], 11)
        self.assertEqual(len(seen), 1)
        products.verify_reuse.assert_not_called()
        products.empty_result.assert_not_called()
        queue = common.read(self.root / "regions" / token / "state/queue.json")
        self.assertEqual(queue["waves"], bundle["waves"])

    def test_no_fire_and_pilot_reuse_take_explicit_terminal_paths(self):
        for token, fires, expected in [("T000177", [], "NO_OBSERVED_FIRE"),
                                       ("T000830", ["2016-02-04"], "REUSED")]:
            bundle = self.make_bundle(token, fires)
            products = types.SimpleNamespace(verify_reuse=mock.Mock(return_value={"reused": True}),
                                             empty_result=mock.Mock(return_value={"days": 0}))
            with mock.patch.object(catalog, "accepted_cube_record", return_value=bundle["record"]), \
                 mock.patch.object(catalog, "prepare_region", return_value=bundle), \
                 mock.patch.object(catalog, "coverage_arrays", return_value={"run_valid_mask": np.ones((96, 96), dtype=bool)}), \
                 mock.patch.dict(sys.modules, {"adapter": types.SimpleNamespace(activate=mock.Mock()), "batch_products": products}):
                result = enroll.enroll_one(self.root, {"token": token})
            self.assertEqual(result["status"], expected)
            if expected == "REUSED":
                products.verify_reuse.assert_called_once()
                products.empty_result.assert_not_called()
            else:
                products.empty_result.assert_called_once()
                products.verify_reuse.assert_not_called()

    def test_scan_skips_admitted_regions_and_recovers_interrupted_scanning(self):
        tokens = ["T000177", "T000178", "T000179"]
        common.atomic(self.root / "state/campaign.json", {"regions": {
            tokens[0]: {"status": "READY"}, tokens[1]: {"status": "SCANNING"},
            tokens[2]: {"status": "WAITING_SOURCE"}}})
        inv = {"accepted": [{"token": token} for token in tokens], "invalid": [],
               "pending": [], "counts": {"accepted": 3, "pending": 0, "invalid": 0}}
        with mock.patch.object(catalog, "inventory", return_value=inv), \
             mock.patch.object(enroll, "enroll_one", return_value={"status": "READY", "selected_days": 11}) as work:
            enroll.scan(self.root)
        self.assertEqual([args[0][1]["token"] for args in work.call_args_list], tokens[1:])
        state = common.read(self.root / "state/campaign.json")
        self.assertTrue(state["scan_complete"])
        self.assertTrue(all(r["status"] == "READY" for r in state["regions"].values()))

    def test_transient_errors_are_bounded_but_deterministic_errors_block_immediately(self):
        token = "T000177"
        inv = {"accepted": [{"token": token}], "invalid": [], "pending": [],
               "counts": {"accepted": 1, "pending": 0, "invalid": 0}}
        common.atomic(self.root / "state/campaign.json", {"regions": {token: {"status": "WAITING_SOURCE"}}})
        with mock.patch.object(catalog, "inventory", return_value=inv), \
             mock.patch.object(enroll, "enroll_one", side_effect=OSError(errno.ESTALE, "temporary source failure")):
            for attempt in range(1, 4):
                enroll.scan(self.root)
                state = common.read(self.root / "state/campaign.json")
                self.assertEqual(state["regions"][token]["source_attempts"], attempt)
                expected = "RETRY_SOURCE" if attempt < 3 else "BLOCKED"
                self.assertEqual(state["regions"][token]["status"], expected)
                state["regions"][token]["retry_after"] = 0
                common.atomic(self.root / "state/campaign.json", state)
        common.atomic(self.root / "state/campaign.json", {"regions": {token: {"status": "WAITING_SOURCE"}}})
        with mock.patch.object(catalog, "inventory", return_value=inv), \
             mock.patch.object(enroll, "enroll_one", side_effect=ValueError("scientific identity changed")):
            enroll.scan(self.root)
        self.assertEqual(common.read(self.root / "state/campaign.json")["regions"][token]["status"], "BLOCKED")

    def test_prepare_scratch_rejects_changed_index_without_overwriting_private_copy(self):
        token = "T000177"
        scratch = Path(self.tmp.name) / "scratch/new_campaign/regions" / token
        source = Path(self.tmp.name) / "readonly_indices"
        source.mkdir()
        index = source / "2014.parquet"
        index.write_bytes(b"audited-index")
        common.atomic(self.root / "manifests/fire_index_source.json", {
            "directory": str(source), "files": {index.name: common.sha(index)}})
        common.atomic(self.root / "regions" / token / "config/deployment.json", {
            "root": str(self.root / "regions" / token), "scratch": str(scratch)})
        enroll.prepare_scratch(self.root, token)
        private = scratch / "cache/fire_index" / index.name
        self.assertEqual(private.read_bytes(), b"audited-index")
        private.write_bytes(b"corrupt-private-copy")
        with self.assertRaisesRegex(ValueError, "Private fire index changed"):
            enroll.prepare_scratch(self.root, token)
        self.assertEqual(index.read_bytes(), b"audited-index")


if __name__ == "__main__":
    unittest.main()
