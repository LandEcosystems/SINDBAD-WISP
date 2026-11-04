"""Report tests use explicitly synthetic fixtures, never production evidence."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from result_analysis.report import (basic_metrics, block_year_interval, completed_regions,
                                    generate, qa_summary, slug)

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_PANDAS = importlib.util.find_spec("pandas") is not None
HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None


class ReportDiscoveryTest(unittest.TestCase):
    def test_only_atomic_completions_and_snapshot_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for rid in ("R1", "R2", "R3"):
                path = root / "regions" / rid
                path.mkdir(parents=True)
                if rid != "R2":
                    (path / "completion.json").write_text("{}")
            self.assertEqual([p.name for p in completed_regions(root)], ["R1", "R3"])
            self.assertEqual([p.name for p in completed_regions(root, {"regions": [{"region_id": "R3"}]})], ["R3"])

    def test_qa_missing_is_not_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "R1"
            path.mkdir()
            rows, counts = qa_summary([path])
            self.assertEqual(counts, {"not_checked": 1})
            self.assertEqual(rows[0]["region_id"], "R1")

    def test_no_input_produces_honest_status(self):
        with tempfile.TemporaryDirectory() as folder:
            result = generate(folder)
            self.assertEqual(result["primary_regions"], 0)
            text = (Path(folder) / "report" / "REPORT_EN.md").read_text()
            self.assertIn("No primary scientific comparison", text)
            self.assertFalse((Path(folder) / "report" / "figures").exists())

    def test_scientific_fail_is_quarantined_not_dropped_without_record(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "regions" / "BAD"
            directory.mkdir(parents=True)
            (directory / "completion.json").write_text("{}")
            (directory / "qa.json").write_text(json.dumps({"status": "fail", "checks": [{"check": "carbon_budget", "status": "fail", "failures": 1}]}))
            result = generate(folder)
            self.assertEqual(result["completed_regions"], 1)
            self.assertEqual(result["primary_regions"], 0)
            self.assertEqual(result["quarantined_regions"][0]["region_id"], "BAD")
            self.assertTrue((directory / "qa.json").exists())

    def test_path_identity(self):
        self.assertEqual(slug("cEco / wood"), "cEco_wood")
        self.assertEqual(slug("../../"), "unknown")


@unittest.skipUnless(HAS_NUMPY, "NumPy required for numerical report checks")
class MetricsTest(unittest.TestCase):
    def test_constant_series_correlations_are_undefined(self):
        result = basic_metrics([0, 0, 0], [0, 0, 0])
        self.assertEqual(result["rmse"], 0)
        self.assertIsNone(result["r"])
        self.assertIsNone(result["nse"])

    def test_metrics_sign_and_nan_pairs(self):
        result = basic_metrics([1, 2, float("nan")], [3, 4, 100])
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["bias"], 2)
        self.assertEqual(result["rmse"], 2)

    def test_block_year_bootstrap_constant_and_reproducible(self):
        args = ([2, 2, 2, 2], [1, 2, 3, 4], ["a", "a", "b", "b"], [2020, 2021, 2020, 2021])
        first = block_year_interval(*args)
        second = block_year_interval(*args)
        self.assertEqual(first, second)
        self.assertEqual(first["lower"], 2)
        self.assertEqual(first["upper"], 2)
        self.assertEqual(first["repeats"], 2000)

    def test_single_block_does_not_get_fake_interval(self):
        result = block_year_interval([1, 2], [1, 1], ["a", "a"], [2020, 2021])
        self.assertEqual(result["estimate"], 1.5)
        self.assertIsNone(result["lower"])
        self.assertIsNone(result["p_exploratory"])

    def test_exploratory_p_requires_blocks_years_and_nonconstant_draws(self):
        import numpy as np
        blocks = np.repeat(np.arange(5), 3)
        years = np.tile(np.arange(3), 5)
        constant = block_year_interval(np.ones(15), np.ones(15), blocks, years)
        self.assertIsNone(constant["p_exploratory"])
        self.assertEqual(constant["p_status"], "undefined_constant_resampling_distribution")
        variable = block_year_interval(np.arange(15), np.ones(15), blocks, years)
        self.assertIsNotNone(variable["p_exploratory"])
        self.assertGreaterEqual(variable["p_exploratory"], 1/2001)
        few_blocks = block_year_interval(np.arange(12), np.ones(12), blocks[:12], years[:12])
        self.assertIsNone(few_blocks["p_exploratory"])

    def test_bh_known_vector_keeps_undefined_tests(self):
        import numpy as np
        from result_analysis.report import benjamini_hochberg
        result = benjamini_hochberg([.01, .04, .03, .002, None])
        np.testing.assert_allclose(result[:4], [.02, .04, .04, .008])
        self.assertIsNone(result[4])


@unittest.skipUnless(HAS_NUMPY and HAS_PANDAS, "NumPy and pandas required")
class TableTest(unittest.TestCase):
    def test_recovery_counts_use_owned_pixels_and_lag_bounds(self):
        import numpy as np
        import pandas as pd
        from result_analysis.report import _owned_recovery_counts
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spatial.npz"
            np.savez(path, first_fire_day=[[0, 1, 4], [2, -1, 1]],
                     fire_day_count=[[1, 1, 2], [2, 0, 1]],
                     run_valid_mask=[[1, 1, 1], [1, 1, 0]],
                     owned_area_m2=[[1., 0., 2.], [3., 1., 1.]])
            rows = [dict(fire_class=state, lag_days=lag, n_pixels=100)
                    for state in ("single_positive_day", "repeated_positive_days", "all") for lag in (-1, 0, 1, 3)]
            result = _owned_recovery_counts(pd.DataFrame(rows), path, 5)
            self.assertEqual(list(result.n_pixels), [0, 1, 1, 1, 2, 2, 1, 0, 2, 3, 2, 1])
            self.assertTrue((result.n_runnable_pixels == 100).all())

    def test_reference_attempts_are_separate_from_primary_region_count(self):
        from result_analysis.report import reference_region_coverage
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            attempted = root / "references" / "A"
            attempted.mkdir(parents=True)
            (attempted / "statuses.json").write_text('{"missing": true}')
            result = reference_region_coverage(root, {"A", "B"})
            self.assertEqual([r["attempted"] for r in result], [True, False])
            self.assertFalse(any(r["pair_file_available"] for r in result))

    def test_overlap_pairs_keep_best_coverage_then_stable_id(self):
        import pandas as pd
        from result_analysis.report import _deduplicated_reference_chunks
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = dict(product="MODIS", variable="gpp", scenario="VIIRS_Fire", reference_cell_id="42",
                        period_start="2015-01-01", period_end="2015-01-09", accepted_90=True,
                        model=12, reference=10, units="g C m-2", carbon_fraction="not_applicable")
            rows = [dict(base, region_id=rid, coverage=coverage) for rid, coverage in (("B", .95), ("C", .99), ("A", .99))]
            path = root / "pairs.csv.gz"
            pd.DataFrame(rows).to_csv(path, index=False, compression="gzip")
            result = pd.concat(list(_deduplicated_reference_chunks([path], root)))
            self.assertEqual(list(result.region_id), ["A"])
            omitted = pd.read_csv(root / "reference_overlap_omissions.csv")
            self.assertEqual(set(omitted.omitted_region), {"B", "C"})
            self.assertTrue((root / "reference_pair_dedup.sqlite").exists())

    def test_partial_year_excluded_and_pair_support_recorded(self):
        import pandas as pd
        from result_analysis.report import _paired
        rows = []
        for year, complete in ((2014, False), (2015, True)):
            for scenario, value in (("No_Fire", 5), ("VIIRS_Fire", 3)):
                rows.append(dict(region_id="A", year=year, scenario=scenario, mean=value,
                                 group_type="overall", group="all", complete_period=complete, valid_area_m2=100))
        result = _paired(pd.DataFrame(rows))
        self.assertEqual(list(result.year), [2015])
        self.assertEqual(list(result.difference), [-2])
        self.assertTrue(result.same_area.all())

    def test_streaming_monthly_rates_respect_actual_day_count(self):
        import pandas as pd
        from result_analysis.report import _monthly_compact
        with tempfile.TemporaryDirectory() as folder:
            region = Path(folder) / "A"
            region.mkdir()
            rows = [dict(region_id="A", scenario="No_Fire", variable="gpp", component="gpp",
                         group_type="overall", group="all", year=2015, month=month, mean=days * 2,
                         valid_area_m2=100, n_days=days, complete_period=True, aggregation="sum")
                    for month, days in ((1, 31), (2, 28))]
            pd.DataFrame(rows).to_csv(region / "monthly.csv", index=False)
            compact = _monthly_compact([region])
            self.assertEqual(list(compact["mean"]), [2, 2])


@unittest.skipUnless(HAS_NUMPY and HAS_PANDAS and HAS_MATPLOTLIB, "Plotting stack required")
class RenderingTest(unittest.TestCase):
    def test_product_dots_preserve_full_scenario_figure_identity(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
        from result_analysis.report import _save_figure
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            figures = []
            with PdfPages(root / "combined.pdf") as pdf:
                for scenario in ("No_Fire", "VIIRS_Fire"):
                    fig, ax = plt.subplots()
                    ax.plot([0, 1], [0, 1])
                    _save_figure(fig, root / ("MOD17A2HGF.061__gpp__" + scenario), pdf, figures, {})
            self.assertEqual(len({row["png"] for row in figures}), 2)
            for row, scenario in zip(figures, ("No_Fire", "VIIRS_Fire")):
                for suffix in ("png", "pdf"):
                    path = Path(row[suffix])
                    self.assertEqual(path.name, "MOD17A2HGF.061__gpp__" + scenario + "." + suffix)
                    self.assertTrue(path.is_file())

    def test_minimal_report_writes_real_assets_and_links(self):
        import numpy as np
        import pandas as pd
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for rid, value in (("A", 2), ("B", 3)):
                directory = root / "regions" / rid
                directory.mkdir(parents=True)
                (directory / "completion.json").write_text("{}")
                (directory / "qa.json").write_text(json.dumps({"status": "pass", "checks": [{"check": "synthetic_fixture", "status": "pass"}]}))
                rows = []
                for scenario, delta in (("No_Fire", 0), ("VIIRS_Fire", -1)):
                    for year in (2014, 2015, 2016):
                        for family, group in (("overall", "all"), ("pyrome", "FIL"), ("vegetation", "forest")):
                            rows.append(dict(region_id=rid, scenario=scenario, year=year, variable="gpp", component="gpp",
                                             group_type=family, group=group, mean=value + delta,
                                             valid_area_m2=400, n_days=365, complete_period=year != 2014,
                                             aggregation="sum", units="g C m-2"))
                pd.DataFrame(rows).to_csv(directory / "annual.csv", index=False)
                np.savez(directory / "spatial.npz", lat=[40, 40.1], lon=[3, 3.1], owned_area_m2=np.full((2, 2), 100.),
                         run_valid_mask=np.ones((2, 2), bool), gpp__gpp__No_Fire=np.full((2, 2), value),
                         gpp__gpp__VIIRS_Fire=np.full((2, 2), value - 1))
            reference_dir = root / "references" / "A"
            reference_dir.mkdir(parents=True)
            refs = [dict(region_id="A", scenario="No_Fire", product="TEST_REFERENCE", variable="gpp",
                         model=float(value), reference=float(value+.5), accepted_90=True, coverage=1,
                         period_start=f"2015-0{month}-01", period_end=f"2015-0{month}-09", reference_cell_id="42",
                         units="g C m-2", carbon_fraction="not_applicable") for month, value in ((1, 1), (2, 2), (3, 3))]
            pd.DataFrame(refs).to_csv(reference_dir / "reference_pairs.csv.gz", index=False, compression="gzip")
            weights = [dict(region_id="A", product="TEST_REFERENCE", variable="gpp", reference_cell_id="42",
                            carbon_fraction="not_applicable", group_type=family, group=group, group_area_m2=100.)
                       for family, group in (("pyrome", "FIL"), ("vegetation", "forest"))]
            pd.DataFrame(weights).to_csv(reference_dir / "reference_group_weights.csv.gz", index=False, compression="gzip")
            report = generate(root, max_components=1)
            self.assertEqual(report["status"], "smoke_test")
            self.assertEqual(report["primary_regions"], 2)
            self.assertGreater(len(report["figures"]), 4)
            self.assertTrue((root / "report" / "REPORT_EN.pdf").stat().st_size > 1000)
            for figure in report["figures"]:
                self.assertTrue(Path(figure["png"]).is_file())
                self.assertTrue(Path(figure["pdf"]).is_file())
            text = (root / "report" / "REPORT_EN.md").read_text()
            self.assertIn("2 pass the primary QA gate", text.replace("**", ""))
            self.assertTrue((root / "report" / "groups" / "pyrome" / "FIL" / "REPORT_EN.md").is_file())
            self.assertTrue((root / "report" / "reference_group_metrics_pooled.csv").is_file())
            pooled = pd.read_csv(root / "report" / "reference_metrics_all_pairs.csv")
            self.assertEqual(int(pooled.iloc[0]["n"]), 3)


if __name__ == "__main__":
    unittest.main()
