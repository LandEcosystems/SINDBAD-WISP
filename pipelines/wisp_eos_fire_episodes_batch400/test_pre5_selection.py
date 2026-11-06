import unittest
import tempfile
from pathlib import Path
import catalog
from pre5_migration import new_manifest

s=catalog.selection

class Pre5SelectionTest(unittest.TestCase):
    def test_inclusive_and_no_trailing_margin(self):
        dates=s.select_dates(['2020-02-29'],margin_after_days=0)
        self.assertEqual([str(d) for d in dates],['2020-02-24','2020-02-25','2020-02-26','2020-02-27','2020-02-28','2020-02-29'])
    def test_other_fire_can_select_post_fire_date(self):
        dates=s.select_dates(['2020-03-01','2020-03-05'],margin_after_days=0)
        self.assertIn(s.as_date('2020-03-03'),dates)
        self.assertNotIn(s.as_date('2020-03-06'),dates)
    def test_period_clip_and_leap_cross_month(self):
        dates=s.select_dates(['2014-01-12','2023-12-31'],margin_after_days=0)
        self.assertEqual(len(dates),7)
        self.assertEqual(str(dates[0]),'2014-01-12')
    def test_subset_partition_and_validation(self):
        fires=['2014-01-12','2014-02-12','2014-02-23','2014-02-28','2014-03-04']
        region=s.build_region_selection('T000001',dict(region_id='test'),fires)
        old=s.make_manifest([region]);new=new_manifest(old,'/tmp/pre5-test','T000001')
        self.assertTrue(s.validate_manifest(new))
        self.assertEqual(new['rules']['margin_after_days'],0)
        for task in new['tasks'].values():
            self.assertEqual(sum(set(task['dates']).issubset(t['dates']) for t in old['tasks'].values()),1)
        selected=new['regions']['T000001']['selected_dates']
        self.assertEqual(selected,[s.timestamp(d) for d in s.select_dates(fires,margin_after_days=0)])
    def test_empty(self):
        old=s.make_manifest([s.build_region_selection('T000001',dict(region_id='test'),[])])
        new=new_manifest(old,'/tmp/pre5-test','T000001')
        self.assertEqual(new['summary']['selected_days'],0)
    def test_negative_after_rejected(self):
        with self.assertRaises(ValueError):s.select_dates(['2019-01-01'],margin_after_days=-1)

if __name__=='__main__':unittest.main()

class InputReuseTest(unittest.TestCase):
    def test_hardlink_reindexes_days_and_reader_verifies_original_arrays(self):
        import numpy as np
        from wisp_batch.store import InputStore
        from wisp_batch.common import SHAPES
        from pre5_migration import hardlink_day
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);shapes={k:(2,3) for k in SHAPES}
            dates=['2019-01-01T00:00:00','2019-01-02T00:00:00']
            old=InputStore(root/'old',dict(name='old'),dates,shapes=shapes,create=True)
            arrays={k:np.full((2,3),i+1,dtype=np.float32) for i,k in enumerate(SHAPES)}
            old.write(1,arrays)
            new=InputStore(root/'new',dict(name='new'),dates[1:],shapes=shapes,create=True)
            hardlink_day(old,new,1,0);hardlink_day(old,new,1,0)
            for k,a in new.read(0).items():self.assertTrue(np.array_equal(a,arrays[k]))
            name=next(iter(SHAPES))
            self.assertEqual((root/'old'/name/'1.0.0').stat().st_ino,(root/'new'/name/'0.0.0').stat().st_ino)
            self.assertNotEqual(old.fingerprint,new.fingerprint)
            for k,a in old.read(1).items():self.assertTrue(np.array_equal(a,arrays[k]))
            new.g[name][0]=np.zeros((2,3),dtype=np.float32)
            with self.assertRaisesRegex(ValueError,'Corrupt input'):new.read(0)
            self.assertTrue(np.array_equal(old.read(1)[name],arrays[name]))

    def test_receipt_wrong_date_rejected(self):
        from pre5_migration import rekey_day
        with self.assertRaises(ValueError):rekey_day(dict(date='x',fingerprint='a',arrays={}), 'y','a','b','source')
