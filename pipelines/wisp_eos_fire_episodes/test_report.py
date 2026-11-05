import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import report as r


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = {'region': 'T000830', 'dates': ['2016-01-30T00:00:00', '2016-01-31T00:00:00']}
        self.put('manifests/selection.json', {
            'tasks': {'T000830_evt_test_B001': self.task},
            'regions': {'T000830': {'summary': {'fire_days': 1, 'selected_days': 2, 'episodes': 1, 'tasks': 1}}}})
        self.put('state/queue.json', {'phase': 'BUILD', 'tasks': {}})

    def put(self, path, value):
        r.atomic(self.root / path, json.dumps(value))

    def test_pending_values_remain_unknown_not_zero(self):
        result = r.build_report(self.root)
        self.assertIsNone(result['cpu']['read_seconds_recorded_events'])
        self.assertIsNone(result['gpu']['gpu_inference_seconds'])
        self.assertIsNone(result['full_decade_comparison']['empirical_speedup'])
        self.assertEqual(result['inputs']['pending_days_without_final_chunk_receipts'], 2)
        self.assertIn('Pending / not measured', r.markdown(result))

    def test_duration_parser(self):
        self.assertEqual(r.seconds('1-02:03:04.5'), 93784.5)
        self.assertEqual(r.seconds('03:04'), 184)
        self.assertIsNone(r.seconds('Unknown'))
        with self.assertRaises(ValueError):
            r.seconds('broken')

    def test_partial_final_line_is_not_complete_record(self):
        path = self.root / 'events.jsonl'
        path.write_bytes(b'{"x":1}\n{"x":')
        rows, partial = r.jsonl(path)
        self.assertEqual(rows, [{'x': 1}])
        self.assertTrue(partial)
        path.write_bytes(b'BROKEN\n{"x":2}\n')
        with self.assertRaises(json.JSONDecodeError):
            r.jsonl(path)

    def test_cpu_snapshots_count_once_gpu_once_per_batch(self):
        self.put('state/job_resources/101.json', {'job': '101', 'elapsed_seconds': 3600,
                 'allocated_cpu': 4, 'cpu_seconds': 360, 'max_aggregate_rss_bytes': 5 * 2**30})
        self.put('state/job_resources/101.json', {'job': '101', 'elapsed_seconds': 7200,
                 'allocated_cpu': 4, 'cpu_seconds': 500, 'max_aggregate_rss_bytes': 5 * 2**30})
        row = {'kind': 'gpu', 'members': [{'date': str(i)} for i in range(8)], 'gpu_seconds': 3.5,
               'h2d_seconds': 1, 'd2h_seconds': 0.5, 'peak_allocated_bytes': 4 * 2**30}
        path = self.root / 'logs/worker_events/gpu_101.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(row) + '\n')
        result = r.build_report(self.root)
        self.assertEqual(result['cpu']['jobs_with_resource_snapshots'], 1)
        self.assertEqual(result['cpu']['worker_elapsed_job_hours_excluding_startup'], 2)
        self.assertEqual(result['cpu']['worker_allocated_core_hours_excluding_startup'], 8)
        self.assertEqual(result['gpu']['gpu_inference_seconds'], 3.5)
        self.assertEqual(result['gpu']['recorded_real_member_predictions_including_retries'], 8)

    def test_receipt_counts_and_future_isolation_are_explicit(self):
        checks = {'future_input_isolation': {'x': True}, 'frozen_reference_parity': {'x': True}}
        self.put('manifests/tasks/T000830_evt_test_B001/input_days.json', {
            '2016-01-30T00:00:00': {'metadata': {'read_seconds': 4, 'assemble_seconds': 1, 'frozen_checks': checks}},
            '2016-01-31T00:00:00': {'metadata': {'read_seconds': 2, 'assemble_seconds': 1}}})
        result = r.build_report(self.root)
        self.assertEqual(result['inputs']['completed_days_with_final_chunk_receipts'], 2)
        self.assertEqual(result['inputs']['recorded_read_seconds_final_receipts'], 6)
        self.assertEqual(result['inputs']['complete_chunk_first_days_with_future_isolation_checks'], 1)
        self.assertTrue(result['inputs']['future_isolation_passed'])

    def test_accounting_groups_jobs_and_preserves_retries(self):
        registered = [{'job': '101', 'kind': 'cpu'}, {'job': '102', 'kind': 'cpu'}, {'job': '103', 'kind': 'gpu'}]
        output = '101|FAILED|3600|4|00:10:00\n102|COMPLETED|1800|4|00:20:00\n103|RUNNING|600|8|00:01:00\n'
        with mock.patch.object(r.subprocess, 'run', return_value=types.SimpleNamespace(stdout=output)):
            raw = r.accounting(registered)
        result = r.summarize_accounting(raw)
        self.assertEqual(result['cpu']['jobs'], 2)
        self.assertEqual(result['cpu']['allocation_job_hours'], 1.5)
        self.assertEqual(result['cpu']['allocated_cpu_core_hours'], 6)
        self.assertEqual(result['gpu']['allocated_gpu_hours_one_device_per_job'], 1 / 6)
        self.assertEqual(result['gpu']['terminal_jobs'], 0)

    def test_wall_time_completed_or_ongoing_does_not_invent_speedup(self):
        self.put('state/submissions/101.json', {'job': '101', 'kind': 'cpu', 'at': '2026-11-05T10:00:00+00:00'})
        current = r.epoch('2026-11-05T11:00:00+00:00')
        first = r.build_report(self.root, now_epoch=current)
        self.assertEqual(first['campaign_wall_seconds_including_queue_and_validation'], 3600)
        self.put('state/queue.json', {'phase': 'COMPLETE', 'tasks': {}, 'completed_at': '2026-11-05T10:30:00+00:00'})
        final = r.build_report(self.root, now_epoch=current)
        self.assertEqual(final['campaign_wall_seconds_including_queue_and_validation'], 1800)
        self.assertIsNone(final['full_decade_comparison']['empirical_speedup'])

    def test_controller_api_atomic_outputs(self):
        result = r.write_report({'root': str(self.root)}, query_sacct=False)
        self.assertEqual(json.loads((self.root / 'reports/resource_report.json').read_text())['phase'], result['phase'])
        self.assertTrue((self.root / 'reports/RESOURCE_REPORT_EN.md').is_file())


if __name__ == '__main__':
    unittest.main()
