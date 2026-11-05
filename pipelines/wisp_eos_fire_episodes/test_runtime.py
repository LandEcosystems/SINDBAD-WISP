"""Lease, reservation and recovery tests; no model or raw datasets are modified."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import runtime as rt


def try_lock(path, pipe):
    try:
        with rt.locked(path, False):
            pipe.send('acquired')
    except BlockingIOError:
        pipe.send('blocked')
    finally:
        pipe.close()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'run'
        self.scratch = Path(self.tmp.name) / 'scratch'
        self.scratch.mkdir()
        self.c = {'root': str(self.root), 'scratch': str(self.scratch),
                  'scratch_budget_bytes': rt.BUDGET_BYTES,
                  'scratch_min_free_bytes': 0, 'shared_cache_reserve_bytes': 2**20}
        self.tasks = {
            'T000830_evt_abc123_B001': self.task('T000830'),
            'T000001_evt_abc123_B001': self.task('T000001'),
        }
        rt.atomic_json(self.root / 'state/queue.json', {'phase': 'BUILD', 'tasks': self.tasks})
        self.q = rt.Queue(self.c)
        rt.STOP = False
        rt._claims.clear()

    @staticmethod
    def task(token):
        return {'region': token, 'kind': 'episode', 'dates': ['2016-01-30T00:00:00', '2016-01-31T00:00:00'],
                'input_status': 'queued', 'prediction_status': 'queued', 'status': 'queued', 'reserved': 0}

    def test_both_regions_claimed_once_and_reservations_retained(self):
        first, task = self.q.claim('cpu')
        second, _ = self.q.claim('cpu')
        self.assertNotEqual(first.split('_')[0], second.split('_')[0])
        self.assertIsNone(self.q.claim('cpu'))
        reservation = task['reserved']
        self.q.update(first, status='ready', job=None, pid=None, input_fingerprint='abc')
        self.q.update(second, status='ready')
        with self.q.edit() as state:
            state['phase'] = 'PREDICT'
        key, _ = self.q.claim('gpu')
        self.q.update(key, status='done', reserved=0)
        final = self.q.snapshot()['tasks'][key]
        self.assertEqual(final['status'], 'done')
        self.assertEqual(final['input_status'], 'ready')
        self.assertEqual(final['reserved'], reservation)
        self.assertIsNone(final['lease'])

    def test_stale_lease_and_wrong_owner_refused(self):
        key, _ = self.q.claim('cpu')
        with self.q.edit() as state:
            state['tasks'][key]['lease'] = 'replacement'
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update(key, status='ready')
        with self.q.edit() as state:
            state['tasks'][key]['lease'] = rt._claims[(str(self.root), key)]
            state['tasks'][key]['pid'] += 1
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update(key, status='ready')

    def test_input_cannot_be_called_prediction_done(self):
        key, _ = self.q.claim('cpu')
        with self.assertRaisesRegex(ValueError, 'stage transition'):
            self.q.update(key, status='done')
        self.assertEqual(self.q.snapshot()['tasks'][key]['input_status'], 'building')

    def test_cache_cap_pauses_without_claim_or_mutation(self):
        small = dict(self.c, scratch_budget_bytes=1)
        self.assertIsNone(rt.Queue(small).claim('cpu'))
        state = self.q.snapshot()
        self.assertIn('cache budget', state['admission_pause']['reason'])
        self.assertTrue(all(t['input_status'] == 'queued' and t['reserved'] == 0 for t in state['tasks'].values()))

    def test_partial_checkpoint_reuses_reservation(self):
        key, first = self.q.claim('cpu')
        self.q.update(key, status='queued', interrupted_at_checkpoint=rt.now())
        resumed, second = self.q.claim('cpu')
        self.assertEqual(resumed, key)
        self.assertEqual(second['reserved'], first['reserved'])
        self.assertNotEqual(second['lease'], first['lease'])
        self.assertEqual(second['claim_generation'], 2)

    def test_actual_scratch_count_never_follows_external_links(self):
        source = Path(self.tmp.name) / 'external'
        source.mkdir()
        (source / 'outside').write_bytes(b'x' * 1000)
        (self.scratch / 'private').write_bytes(b'x' * 7)
        (self.scratch / 'link').symlink_to(source, target_is_directory=True)
        self.assertEqual(rt.scratch_size(self.scratch), 7)

    def test_contiguous_dates_and_identity_required(self):
        key = 'T000830_evt_abcd_B001'
        good = self.task('T000830')
        rt.validate_task(key, good)
        for mutation in [{'region': 'T000001'}, {'kind': 'month'},
                         {'dates': ['2016-01-30T00:00:00', '2016-02-01T00:00:00']},
                         {'dates': ['2016-01-30T12:00:00']}, {'dates': []},
                         {'dates': ['2014-01-11T00:00:00']}]:
            with self.assertRaises(ValueError):
                rt.validate_task(key, dict(good, **mutation))

    def test_posix_lock_covers_other_process_and_is_stable(self):
        path = self.root / 'locks/test.lock'
        parent, child = multiprocessing.get_context('fork').Pipe()
        with rt.locked(path):
            inode = path.stat().st_ino
            worker = multiprocessing.get_context('fork').Process(target=try_lock, args=(path, child))
            worker.start()
            self.assertEqual(parent.recv(), 'blocked')
            worker.join(5)
            self.assertEqual(worker.exitcode, 0)
            with self.assertRaisesRegex(RuntimeError, 'Nested'):
                with rt.locked(path):
                    pass
        with rt.locked(path, False):
            self.assertEqual(path.stat().st_ino, inode)

    def test_recovery_requires_scheduler_terminal_owner(self):
        key, _ = self.q.claim('cpu')
        with self.q.edit() as state:
            state['tasks'][key]['job'] = '123'
        response = types.SimpleNamespace(stdout='123|RUNNING\n')
        with mock.patch.object(rt.subprocess, 'run', return_value=response):
            rt.recover(self.c, [])
        self.assertEqual(self.q.snapshot()['tasks'][key]['input_status'], 'building')
        response.stdout = '123|TIMEOUT\n'
        with mock.patch.object(rt.subprocess, 'run', return_value=response):
            rt.recover(self.c, [])
        self.assertEqual(self.q.snapshot()['tasks'][key]['input_status'], 'queued')
        self.assertIsNone(self.q.snapshot()['tasks'][key]['lease'])

    def test_deterministic_failure_not_retried_transient_bounded(self):
        key, _ = self.q.claim('cpu')
        self.q.update(key, status='blocked', transient=False, attempts=1, failed_stage='cpu')
        rt.recover(self.c, [])
        self.assertEqual(self.q.snapshot()['tasks'][key]['input_status'], 'blocked')
        with self.q.edit() as state:
            state['tasks'][key].update(transient=True, attempts=3, retry_after=0)
        rt.recover(self.c, [])
        self.assertEqual(self.q.snapshot()['tasks'][key]['input_status'], 'blocked')
        with self.q.edit() as state:
            state['tasks'][key].update(attempts=2)
        rt.recover(self.c, [])
        self.assertEqual(self.q.snapshot()['tasks'][key]['input_status'], 'queued')

    def test_operational_changes_do_not_permit_scientific_changes(self):
        original = dict(self.c, root='/original/root', scratch='/different/scratch',
                        scratch_budget_bytes=2**40, source='frozen', precision='bfloat16')
        current = dict(self.c, source='frozen', precision='bfloat16')
        with mock.patch.object(rt, 'ROOT', self.root):
            rt.check_operational_config(current, original)
            with self.assertRaisesRegex(ValueError, 'scientific'):
                rt.check_operational_config(dict(current, precision='float32'), original)
            with self.assertRaisesRegex(ValueError, 'independent'):
                rt.check_operational_config(dict(current, scratch='/different/scratch/events'), original)
            with self.assertRaisesRegex(ValueError, '128 GiB'):
                rt.check_operational_config(dict(current, scratch_budget_bytes=2**40), original)


if __name__ == '__main__':
    unittest.main()
