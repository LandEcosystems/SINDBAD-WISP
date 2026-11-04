import unittest
from dual_campaign import partitions, keep_allocation, validate_lanes

class DualTests(unittest.TestCase):
    def setUp(self):
        self.tokens = ['T%06d' % i for i in range(16)]
    def test_disjoint_complete_order(self):
        lanes = partitions(self.tokens)
        self.assertFalse(set(lanes['a']) & set(lanes['b']))
        self.assertEqual(set(lanes['a'] + lanes['b']), set(self.tokens))
        self.assertEqual(lanes['a'][0], self.tokens[0])
        self.assertEqual(lanes['b'][0], self.tokens[1])
    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            partitions(self.tokens[:-1] + self.tokens[:1])
    def test_scratch_counted_once(self):
        b = partitions(self.tokens)['b']
        for key in [t + '_2019-12' for t in self.tokens] + ['unregistered', 'v_T099999_day']:
            self.assertEqual(sum(keep_allocation('/scratch/' + key, lane, b) for lane in ['a', 'b']), 1)
    def test_budget(self):
        lanes = partitions(self.tokens)
        m = dict(original_tokens=self.tokens, cpu_jobs_max=120, scratch_budget_bytes=2*2**40,
                 lanes={k: dict(tokens=v, root='/run/' + k) for k,v in lanes.items()})
        self.assertEqual(validate_lanes(m), lanes)
        m['lanes']['b']['root'] = '/run/a'
        with self.assertRaises(ValueError):
            validate_lanes(m)

if __name__ == '__main__':
    unittest.main()
