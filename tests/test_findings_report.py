import unittest
import numpy as np
import pandas as pd
from result_analysis.findings_report import region_means, weighted_summary, choose_cases


class FindingsTests(unittest.TestCase):
    def frame(self):
        rows=[]
        for region,area in [('a',1.),('b',9.)]:
            for year in range(2014,2024):
                for scenario in ['No_Fire','VIIRS_Fire']:
                    rows.append(dict(region_id=region,group_type='overall',group='all',year=year,
                        scenario=scenario,mean=100. if scenario=='No_Fire' else (90. if region=='a' else 99.),
                        valid_area_m2=area,complete_period=year!=2014))
        return pd.DataFrame(rows)

    def test_each_region_once_and_partial_year_excluded(self):
        f=self.frame();f.loc[f.year==2014,'mean']=1000000.
        result=region_means(f)
        self.assertEqual(len(result),2)
        self.assertEqual(set(result.n_years),{9})
        self.assertEqual(result.set_index('region_id').loc['a','No_Fire'],100.)
        self.assertEqual(result.set_index('region_id').loc['a','difference'],-10.)

    def test_missing_year_or_changed_support_refused(self):
        f=self.frame();f=f[~((f.region_id=='a')&(f.year==2015))]
        with self.assertRaises(ValueError):region_means(f)
        f=self.frame();f.loc[(f.scenario=='VIIRS_Fire')&(f.year==2015),'valid_area_m2']*=2
        with self.assertRaises(ValueError):region_means(f)

    def test_area_weighted_effect_not_unweighted_percent(self):
        s=weighted_summary(region_means(self.frame())).iloc[0]
        self.assertAlmostEqual(s.difference,-1.9)
        self.assertAlmostEqual(s.relative_change_pct,-1.9)
        self.assertEqual(s.n_regions,2)

    def test_exploratory_case_selection_excludes_tiny_baselines(self):
        f=pd.DataFrame(dict(region_id=['tiny','a','b','c'],final__cVeg__No_Fire=[.1,200,300,400],
            area_m2=[2e8]*4,final_cVeg_relative_pct=[-100,-5,-10,-1]))
        self.assertEqual(choose_cases(f).region_id.tolist(),['b','a','c'])


if __name__=='__main__':unittest.main()
