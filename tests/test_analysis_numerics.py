import unittest
import numpy as np
import pandas as pd
from result_analysis.numerics import *

class NumericalContracts(unittest.TestCase):
    def test_budget_starts_after_first_saved_state(self):
        stock=np.array([[[10.,8.,5.]]]); n=np.array([[[999.,1.,2.]]]); f=np.array([[[888.,1.,1.]]]); z=np.zeros_like(stock)
        self.assertEqual(budget_check(stock,n,f,z,z,z,[[True]])['failures'],0)
    def test_metrics_signed_and_constant(self):
        a=metrics([1,2,3],[2,3,4]);self.assertEqual(a['bias'],1);self.assertAlmostEqual(a['r2'],1);self.assertLess(a['nse'],1)
        self.assertTrue(np.isnan(metrics([0,0],[0,0])['r']))
    def test_missing_no_skipna(self):
        self.assertTrue(np.isnan(period_reduce(np.array([[1,np.nan]]),[0,1],'flux')[0]))
    def test_leap_final_modis_window(self):
        g=time_groups(pd.date_range('2020-01-01','2020-12-31'))
        self.assertEqual(len(g['composite'][-1][1]),6)
    def test_area_and_finite_water(self):
        self.assertEqual(weighted_mean(np.array([10,0]),np.array([2,0])),(10,2))
    def test_rounding(self):
        self.assertEqual(rounding_bound(0),0)
        self.assertGreater(rounding_bound(8000),.01)
if __name__=='__main__':unittest.main()
