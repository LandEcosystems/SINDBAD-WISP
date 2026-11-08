import unittest
from qualified_sbatch import restricted_args


class QualifiedHostsTests(unittest.TestCase):
    def test_cpu_excludes_unknown_and_failed_hosts(self):
        args = restricted_args(['--job-name=w4e-cpu', '--partition=work,small,gpu', 'run.sh'],
                               {'qualified_hosts': ['good']}, ['good', 'bad', 'new'])
        self.assertEqual(args[0], '--exclude=bad,new')

    def test_gpu_and_other_projects_unchanged(self):
        for name in ['w4e-gpu', 'w4e-control', 'wisp2c-gpu', 'another-project']:
            original = ['--job-name=' + name, '--gres=gpu:A100:1', 'run.sh']
            self.assertEqual(restricted_args(original, {}, []), original)

    def test_verifier_also_restricted(self):
        self.assertEqual(restricted_args(['-J', 'w4e-verify', 'run.sh'],
                         {'qualified_hosts': ['good']}, ['bad', 'good'])[0], '--exclude=bad')

    def test_existing_exclusions_preserved(self):
        args = restricted_args(['--job-name=w4e-cpu', '--exclude=good', '-x', 'bad', 'run.sh'],
                               {'qualified_hosts': ['good']}, ['good', 'new'])
        self.assertEqual(args, ['--exclude=new,good,bad', '--job-name=w4e-cpu', 'run.sh'])

    def test_no_matching_host_refused(self):
        with self.assertRaises(ValueError):
            restricted_args(['--job-name=w4e-cpu'], {'qualified_hosts': ['gone']}, ['new'])

    def test_partition_without_any_qualified_host_is_removed(self):
        original = ['--job-name=w4e-cpu', '--partition=work,small,gpu', 'run.sh']
        actual = restricted_args(original, {'qualified_hosts': ['good', 'smallgood']},
                                 ['good', 'smallgood', 'bad'],
                                 {'work': ['good'], 'small': ['smallgood'], 'gpu': ['bad']})
        self.assertIn('--partition=work,small', actual)
        self.assertNotIn('--partition=work,small,gpu', actual)

    def test_qualified_gpu_node_keeps_gpu_partition_for_cpu(self):
        actual = restricted_args(['-J', 'w4e-verify', '-p', 'gpu', 'run.sh'],
                                 {'qualified_hosts': ['good']}, ['good'], {'gpu': ['good']})
        self.assertIn('--partition=gpu', actual)

    def test_no_qualified_partition_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'No requested partition'):
            restricted_args(['-J', 'w4e-cpu', '-p', 'gpu'],
                            {'qualified_hosts': ['good']}, ['good', 'bad'],
                            {'work': ['good'], 'gpu': ['bad']})


if __name__ == '__main__':
    unittest.main()
