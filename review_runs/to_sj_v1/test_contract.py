"""Regression checks for the sentinel/mask adapter and strict artifact gate."""
import tempfile
import unittest
from pathlib import Path
import numpy as np
import netCDF4 as nc
from PIL import Image
from prepare import make_adapter
from validate import png, require_file, canonical

class Contract(unittest.TestCase):
    def test_unknown_and_excluded_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'adapter.nc'
            make_adapter(p,np.array([1.,2.]),np.array([3.,4.]),
                         np.array([[9999,2013],[2000,1852]]),np.array([[True,True],[False,True]]))
            with nc.Dataset(p) as ds:
                a=np.ma.filled(ds['disturbance_year_model'][:],np.nan)
                self.assertEqual(a[0,0],9999);self.assertEqual(a[1,1],1852)
                self.assertTrue(np.isnan(a[1,0]))
    def test_png_rejects_missing_empty_and_corrupt(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'x.png'
            with self.assertRaises(AssertionError):require_file(p)
            p.touch()
            with self.assertRaises(AssertionError):require_file(p)
            p.write_bytes(b'not an image')
            with self.assertRaises(Exception):png(p)
            Image.new('RGB',(200,200),'white').save(p)
            self.assertGreater(png(p)['bytes'],0)
    def test_saved_dimension_orientation(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'saved.nc'
            with nc.Dataset(p,'w') as ds:
                for n,k in [('lon',3),('lat',2),('layer',4),('time',5)]:ds.createDimension(n,k)
                v=ds.createVariable('test','f4',('lon','lat','layer','time'))
                a=np.arange(120).reshape(3,2,4,5);v[:]=a
                got=canonical(v,(slice(None),)*4)
                np.testing.assert_array_equal(got,a.transpose(3,2,1,0))
if __name__=='__main__':unittest.main()
