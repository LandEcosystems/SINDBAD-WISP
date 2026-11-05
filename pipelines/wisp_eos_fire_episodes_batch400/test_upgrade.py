"""Quiescent operational upgrades preserve science and resume atomic swaps."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import common
import upgrade


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "campaign"
        self.source = base / "checkout" / "batch400"
        self.source.mkdir(parents=True)
        pilot = self.source.parent / "wisp_eos_fire_episodes"
        pilot.mkdir()
        (pilot / "runtime.py").write_text("# frozen pilot dependency\n")
        (self.source / "common.py").write_text("# new operational implementation\n")
        code = self.root / "code"
        (code / "batch400").mkdir(parents=True)
        (code / "wisp_eos_fire_episodes").mkdir()
        (code / "batch400/common.py").write_text("# old operational implementation\n")
        (code / "wisp_eos_fire_episodes/runtime.py").write_bytes((pilot / "runtime.py").read_bytes())
        (code / "run.sh").write_text("#!/bin/sh\nexec frozen-python controller.py\n")
        for name in upgrade.FROZEN_PATHS:
            common.atomic(self.root / name, {"frozen": name})
        common.atomic(self.root / "manifests/deployment.json", {"fingerprint": "scientific-frozen"})
        common.atomic(self.root / "config/campaign.json", {"git_commit": "a" * 40, "science": "unchanged"})
        common.atomic(self.root / "state/campaign.json", {
            "lanes": [None] * 10, "pilot_passed": False,
            "regions": {"T000001": {"status": "REUSED"}, "T000002": {"status": "READY"},
                        "T000003": {"status": "SCANNING"}}})
        files = {str(p.relative_to(code)): common.sha(p) for p in code.rglob("*") if p.is_file()}
        self.release = {"git_commit": "a" * 40, "files": files,
                        "campaign_config_sha256": common.sha(self.root / "config/campaign.json"),
                        "catalog_sha256": common.sha(self.root / "manifests/named_production_regions.json"),
                        "source_files": {p: common.sha(self.root / p) for p in upgrade.FROZEN_PATHS[1:]}}
        common.atomic(code / "release.json", self.release)
        self.old_release_sha = common.sha(code / "release.json")
        self.identities = {}
        for token in ("T000001", "T000002"):
            rr = self.root / "regions" / token
            for field, relative in upgrade.IDENTITY_PATHS.items():
                common.atomic(rr / relative, {"frozen": relative})
            qa = rr / "qa/coverage.npz"
            qa.parent.mkdir(parents=True)
            qa.write_bytes(b"frozen coverage")
            common.atomic(rr / "manifests/regions.json", {token: {"coverage_qa": str(qa)}})
            common.atomic(rr / "manifests/deployment.json", {"fingerprint": "scientific-frozen"})
            common.atomic(rr / "state/queue.json", {"phase": "BUILD", "active_wave": 0,
                "tasks": {"task": {"input_status": "queued", "prediction_status": "queued", "reserved": 0}}})
            identity = {field: common.sha(rr / relative) for field, relative in upgrade.IDENTITY_PATHS.items()}
            identity.update(campaign_release_sha256=self.old_release_sha,
                            deployment_fingerprint="scientific-frozen", coverage_qa_sha256=common.sha(qa))
            common.atomic(rr / "manifests/identity.json", identity)
            self.identities[token] = copy.deepcopy(identity)
        self.new_commit = "b" * 40

    def test_success_archives_old_code_and_changes_only_release_binding(self):
        before = {name: (self.root / name).read_bytes() for name in upgrade.FROZEN_PATHS + ["state/campaign.json"]}
        result = upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["region_identities_rebound"], 2)
        archive = Path(result["archive"])
        self.assertEqual(common.sha(archive / "code/release.json"), self.old_release_sha)
        self.assertEqual((self.root / "code/batch400/common.py").read_text(), "# new operational implementation\n")
        self.assertEqual(common.read(self.root / "config/campaign.json")["git_commit"], "a" * 40)
        self.assertEqual(common.read(self.root / "code/release.json")["git_commit"], self.new_commit)
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        for token, old in self.identities.items():
            current = common.read(self.root / "regions" / token / "manifests/identity.json")
            self.assertEqual(current, dict(old, campaign_release_sha256=result["new_release_sha256"]))
            self.assertEqual(common.read(archive / "region_identities" / (token + ".json")), old)
        self.assertEqual(upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[]), result)

    def test_rejects_any_live_campaign_job_but_ignores_unrelated_work(self):
        for rows in [[{"id": "123", "name": "w4e-scan", "state": "RUNNING"}],
                     [{"id": "124", "name": "w4e-control", "state": "PENDING"}]]:
            with self.assertRaisesRegex(ValueError, "still active"):
                upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=rows)
        common.atomic(self.root / "state/submissions/999.json", {"kind": "scan"})
        with self.assertRaisesRegex(ValueError, "still active"):
            upgrade.upgrade(self.source, self.root, self.new_commit,
                            slurm_rows=[{"id": "999", "name": "renamed-job", "state": "RUNNING"}])
        self.assertFalse((self.root / "releases").exists())
        result = upgrade.upgrade(self.source, self.root, self.new_commit,
                                 slurm_rows=[{"id": "33", "name": "other-experiment", "state": "RUNNING"}])
        self.assertEqual(result["phase"], "COMPLETE")

    def test_rejects_lane_or_started_input_even_without_slurm_jobs(self):
        state_path = self.root / "state/campaign.json"
        state = common.read(state_path)
        state["lanes"][0] = "T000002"
        common.atomic(state_path, state)
        with self.assertRaisesRegex(ValueError, "Production has been admitted"):
            upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        state["lanes"][0] = None
        common.atomic(state_path, state)
        queue_path = self.root / "regions/T000002/state/queue.json"
        queue = common.read(queue_path)
        queue["tasks"]["task"]["input_status"] = "ready"
        common.atomic(queue_path, queue)
        with self.assertRaisesRegex(ValueError, "active or started"):
            upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])

    def test_rejects_changed_dependency_or_frozen_source_before_swap(self):
        pilot = self.source.parent / "wisp_eos_fire_episodes/runtime.py"
        pilot.write_text("# modified numerical dependency\n")
        with self.assertRaisesRegex(ValueError, "pilot dependency differs"):
            upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        pilot.write_text("# frozen pilot dependency\n")
        common.atomic(self.root / "regions/T000002/config/deployment.json", {"altered": True})
        with self.assertRaisesRegex(ValueError, "Frozen region identity changed"):
            upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        self.assertEqual(common.sha(self.root / "code/release.json"), self.old_release_sha)

    def test_recovers_interruption_between_directory_swaps(self):
        original_replace = os.replace
        def interrupt(source, target):
            if Path(source).name == "staged_code" and Path(target) == self.root / "code":
                raise OSError("simulated interruption after archive rename")
            return original_replace(source, target)
        with mock.patch.object(upgrade.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        self.assertFalse((self.root / "code").exists())
        result = upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        self.assertEqual(result["phase"], "COMPLETE")
        common.verify_release(self.root)

    def test_recovers_interruption_after_one_identity_was_rebound(self):
        original_atomic = upgrade.atomic
        target = self.root / "regions/T000002/manifests/identity.json"
        def interrupt(path, value):
            if Path(path) == target and value.get("campaign_release_sha256") != self.old_release_sha:
                raise OSError("simulated second-identity interruption")
            return original_atomic(path, value)
        with mock.patch.object(upgrade, "atomic", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "second-identity"):
                upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        journal = common.read(self.root / "state/operational_upgrade.json")
        first = common.read(self.root / "regions/T000001/manifests/identity.json")
        second = common.read(target)
        self.assertEqual(first["campaign_release_sha256"], journal["new_release_sha256"])
        self.assertEqual(second["campaign_release_sha256"], journal["old_release_sha256"])
        result = upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        self.assertEqual(result["phase"], "COMPLETE")

    def test_resume_refuses_changed_staged_source(self):
        with mock.patch.object(upgrade, "_finish", side_effect=OSError("before swap")):
            with self.assertRaises(OSError):
                upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])
        (self.source / "common.py").write_text("# changed during interrupted upgrade\n")
        with self.assertRaisesRegex(ValueError, "same frozen source"):
            upgrade.upgrade(self.source, self.root, self.new_commit, slurm_rows=[])


if __name__ == "__main__":
    unittest.main()
