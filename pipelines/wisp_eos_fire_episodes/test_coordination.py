"""No Slurm calls: verify shared GPU exclusion and legacy stop boundaries."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import coordination


class CoordinationTests(unittest.TestCase):
    def test_gpu_busy_includes_queued_gpu_from_all_three_workflows(self):
        for name in ("wisp1c-gpu", "wisp2c-gpu", "wispev-gpu"):
            self.assertTrue(coordination.gpu_busy([{"name": name, "state": "PENDING"}]))
        self.assertFalse(coordination.gpu_busy([{"name": "wispev-cpu", "state": "RUNNING"}]))
        self.assertFalse(coordination.gpu_busy([]))

    def test_continuation_stops_after_current_and_defers_rest(self):
        assigned = ["T000830", "T000010", "T000020"]
        completed, deferred = coordination.continuation_transition(
            {"phase": "CLEANED", "active_cube": "T000830", "completed_cubes": []}, assigned)
        self.assertEqual(completed, ["T000830"])
        self.assertEqual(deferred, ["T000010", "T000020"])
        # An already recorded first completion is idempotent, not a second admission.
        self.assertEqual(coordination.continuation_transition(
            {"phase": "CLEANED", "active_cube": "T000830", "completed_cubes": ["T000830"]}, assigned),
                         (completed, deferred))

    def test_continuation_refuses_unfinished_or_advanced_campaign(self):
        assigned = ["T000830", "T000010"]
        for state in [
            {"phase": "BUILD", "active_cube": "T000830"},
            {"phase": "CLEANED", "active_cube": "T000010"},
            {"phase": "CLEANED", "active_cube": "T000830", "completed_cubes": assigned},
        ]:
            with self.assertRaises(ValueError):
                coordination.continuation_transition(state, assigned)

    def test_release_covers_operational_configuration_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = root / "code"
            code.mkdir()
            (root / "config").mkdir()
            (root / "manifests").mkdir()
            (code / "file.py").write_text("immutable code\n")
            (root / "config/deployment.json").write_text("{}\n")
            (root / "manifests/selection.json").write_text("{}\n")
            release = {"files": {"file.py": coordination.sha(code / "file.py")},
                       "config_sha256": coordination.sha(root / "config/deployment.json"),
                       "selection_sha256": coordination.sha(root / "manifests/selection.json")}
            (code / "release.json").write_text(json.dumps(release))
            self.assertEqual(coordination.verify_release(code), release)
            (root / "manifests/selection.json").write_text('{"changed": true}\n')
            with self.assertRaisesRegex(ValueError, "deployment changed"):
                coordination.verify_release(code)


def load_controller_with_fake_runtime():
    """Avoid importing cluster Torch/CUDA modules for orchestration unit tests."""
    rt = types.ModuleType("runtime")
    rt.now = lambda: "2026-11-05T12:00:00Z"
    rt.locked = lambda *a, **k: contextlib.nullcontext()
    rt.read_json = lambda path: json.loads(Path(path).read_text())
    package = types.ModuleType("wisp_batch")
    common = types.ModuleType("wisp_batch.common")

    def atomic_json(path, value):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(value))

    common.atomic_json = atomic_json
    common.file_hash = lambda path: "f" * 64
    spec = importlib.util.spec_from_file_location("_event_controller_unit", Path(__file__).with_name("controller.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"runtime": rt, "wisp_batch": package, "wisp_batch.common": common}):
        spec.loader.exec_module(module)
    return module, rt


class ControllerSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.ctl, self.rt = load_controller_with_fake_runtime()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rt.HERE = self.root / "code"
        self.rt.HERE.mkdir()
        self.config = {"root": str(self.root)}

    def test_event_gpu_waits_for_legacy_gpu(self):
        with patch.object(self.ctl, "jobs", return_value=[]), \
                patch.object(self.ctl, "gpu_busy", return_value=True), \
                patch.object(self.ctl.subprocess, "run") as run:
            self.assertIsNone(self.ctl.submit(self.config, "gpu"))
            run.assert_not_called()

    def test_cpu_job_cap_counts_pending_jobs(self):
        current = [{"kind": "cpu", "state": "PENDING"} for _ in range(32)]
        with patch.object(self.ctl, "jobs", return_value=current), \
                patch.object(self.ctl.subprocess, "run") as run:
            self.assertIsNone(self.ctl.submit(self.config, "cpu"))
            run.assert_not_called()

    def test_cpu_submits_only_requested_resources_without_gpu_device(self):
        locked = []

        @contextlib.contextmanager
        def lock(path):
            locked.append(Path(path))
            yield

        response = types.SimpleNamespace(stdout="98765;cluster\n")
        with patch.object(self.ctl, "jobs", return_value=[]), \
                patch.object(self.rt, "locked", side_effect=lock), \
                patch.object(self.ctl.subprocess, "run", return_value=response) as run:
            self.assertEqual(self.ctl.submit(self.config, "cpu"), "98765")
        args = run.call_args.args[0]
        self.assertIn("--cpus-per-task=4", args)
        self.assertIn("--mem=16G", args)
        self.assertIn("--partition=work,small,gpu", args)
        self.assertIn("--time=24:00:00", args)
        self.assertFalse(any("gres" in item or "big" in item for item in args))
        self.assertEqual(locked, [coordination.GPU_SUBMIT_LOCK])
        receipt = json.loads((self.root / "state/submissions/98765.json").read_text())
        self.assertEqual(receipt["kind"], "cpu")
        self.assertEqual(receipt["job"], "98765")

    def test_gpu_submission_requests_exactly_one_a100(self):
        response = types.SimpleNamespace(stdout="98766\n")
        with patch.object(self.ctl, "jobs", return_value=[]), \
                patch.object(self.ctl, "gpu_busy", return_value=False), \
                patch.object(self.ctl.subprocess, "run", return_value=response) as run:
            self.ctl.submit(self.config, "gpu")
        args = run.call_args.args[0]
        self.assertEqual([a for a in args if a.startswith("--gres=")], ["--gres=gpu:A100:1"])

    def test_controller_duplicate_refused_but_dependent_renewal_allowed(self):
        current = [{"kind": "cpu", "state": "RUNNING", "id": str(i)} for i in range(30)]
        current.append({"kind": "control", "state": "RUNNING", "id": "12345"})
        response = types.SimpleNamespace(stdout="98767\n")
        with patch.object(self.ctl, "jobs", return_value=current), \
                patch.object(self.ctl.subprocess, "run", return_value=response) as run:
            with self.assertRaisesRegex(ValueError, "controller already"):
                self.ctl.submit(self.config, "control")
            run.assert_not_called()
            self.assertEqual(self.ctl.submit(self.config, "control", dependency="12345"), "98767")
            self.assertIn("--dependency=afterany:12345", run.call_args.args[0])

    def test_build_leaves_slot_for_controller_renewal(self):
        state = {"phase": "BUILD", "tasks": {
            str(i): {"input_status": "queued", "prediction_status": "queued"} for i in range(68)}}
        queue = types.SimpleNamespace(snapshot=lambda: state)
        current = [{"kind": "control", "id": "12345", "state": "RUNNING"}]
        submitted = []
        with patch.object(self.rt, "Queue", return_value=queue, create=True), \
                patch.object(self.rt, "STOP", False, create=True), \
                patch.object(self.rt, "recover", create=True), \
                patch.object(self.ctl, "jobs", return_value=current), \
                patch.object(self.ctl, "pilot_gate", return_value=True), \
                patch.object(self.ctl, "status"), \
                patch.object(self.ctl, "submit", side_effect=lambda c, kind: submitted.append(kind) or "99999"):
            self.ctl.control(self.config, once=True)
        self.assertEqual(submitted, ["cpu"] * 30)


class InstallPureTests(unittest.TestCase):
    def test_parity_dates_include_both_regions_and_boundaries(self):
        import install
        import selection
        regions = [selection.build_region_selection(token, {"region_id": token},
                   ["2016-02-04", "2016-02-07", "2023-12-30"]) for token in selection.TOKENS]
        manifest = selection.make_manifest(regions)
        planned = install.choose_parity(manifest)
        self.assertEqual(set(planned), set(selection.TOKENS))
        for token, days in planned.items():
            self.assertEqual(len(days), 4)
            self.assertEqual(len(set(days)), 4)
            self.assertIn("2016-01-30T00:00:00", days)
            self.assertIn("2023-12-31T00:00:00", days)
            self.assertIn("2016-02-01T00:00:00", days)
            self.assertTrue(set(days).issubset(manifest["regions"][token]["selected_dates"]))

    def test_launcher_changes_only_operational_cache_and_entrypoint(self):
        import install
        template = ('#!/bin/bash\nexport PYTHONPATH=/frozen/app\n'
                    'export WISP_CACHE=/Net/Groups/BGI/tscratch/ybai/wisp_eos_batch1_v1/cache\n'
                    'export OMP_NUM_THREADS=2\nexec /old/python old.py "$1"\n')
        launched = install.launcher(template, '/python /new/controller.py "$1"', Path('/private/cache'))
        self.assertIn('export PYTHONPATH=/frozen/app', launched)
        self.assertIn('export OMP_NUM_THREADS=2', launched)
        self.assertIn('WISP_CACHE=/private/cache', launched)
        self.assertTrue(launched.endswith('exec /python /new/controller.py "$1"\n'))
        self.assertNotIn('/old/python', launched)


if __name__ == "__main__":
    unittest.main()
