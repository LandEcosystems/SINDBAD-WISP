import unittest

import numpy as np
import pandas as pd

from result_analysis.inclusive_report import allowed_note, reduce_region


class InclusiveReportTests(unittest.TestCase):
    def test_only_tiny_documented_water_negatives_are_notes(self):
        self.assertTrue(allowed_note("No_Fire/TWS/soil_1/nonnegative", -3.82e-6))
        self.assertTrue(allowed_note("VIIRS_Fire/evapotranspiration/evapotranspiration/nonnegative", -5.64e-8))
        self.assertFalse(allowed_note("No_Fire/TWS/soil_1/nonnegative", -.1))
        self.assertFalse(allowed_note("No_Fire/cEco/cVeg/nonnegative", -1e-8))
        self.assertFalse(allowed_note("No_Fire/TWS/soil_1/finite", np.nan))

    def frame(self):
        return pd.DataFrame([
            dict(region_id="a", variable="gpp", component="gpp", group_type="overall", group="all",
                 year=year, aggregation="sum", units="g C m-2", scenario=scenario,
                 mean=1000 if year == 2014 else (100 if scenario == "No_Fire" else 90),
                 valid_area_m2=2., complete_period=year != 2014)
            for year in range(2014, 2024) for scenario in ("No_Fire", "VIIRS_Fire")])

    def test_reduction_excludes_partial_year_and_keeps_paired_response(self):
        mean, annual = reduce_region(self.frame())
        self.assertEqual(len(mean), 1)
        self.assertEqual(len(annual), 9)
        self.assertEqual(mean.No_Fire.iloc[0], 100)
        self.assertEqual(mean.difference.iloc[0], -10)

    def test_missing_year_and_unmatched_support_are_not_accepted(self):
        f = self.frame()
        with self.assertRaises(AssertionError): reduce_region(f[f.year != 2015])
        f.loc[(f.year == 2015) & (f.scenario == "VIIRS_Fire"), "valid_area_m2"] = 3
        with self.assertRaises(AssertionError): reduce_region(f)


if __name__ == "__main__":
    unittest.main()
