"""Synthetic fixtures for safe parallel rendering; never production findings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from result_analysis import report
from result_analysis.parallel_report import (prepare, worker, merge, merge_pdfs, validated_pdf,
    select_main_figures, verify_task, _legacy_pair_matches, destination)


def fixture(root):
    (root / "inventory.json").write_text(json.dumps({"regions": [{"region_id": "A"}, {"region_id": "B"}], "source_metadata_fingerprint": "synthetic"}))
    for rid, base in [("A", 3.1), ("B", 4.3)]:
        directory = root / "regions" / rid; directory.mkdir(parents=True)
        (directory / "completion.json").write_text('{"fingerprint":"synthetic"}')
        (directory / "qa.json").write_text('{"status":"pass","checks":[]}')
        rows = []
        for scenario, offset in [("No_Fire", 0), ("VIIRS_Fire", -.4)]:
            for year in [2014, 2015, 2016]:
                for family, group in [("overall", "all"), ("pyrome", "FIL"), ("vegetation", "forest")]:
                    rows.append(dict(region_id=rid, scenario=scenario, year=year, variable="gpp", component="gpp", group_type=family, group=group,
                                     mean=base+offset+year%2/7, valid_area_m2=400., n_days=365, complete_period=year!=2014, aggregation="sum", units="g C m-2"))
        pd.DataFrame(rows).to_csv(directory / "annual.csv", index=False)
        np.savez(directory / "spatial.npz", lat=[40., 40.1], lon=[3., 3.1], owned_area_m2=np.full((2,2),100.),
                 run_valid_mask=np.ones((2,2), bool), gpp__gpp__No_Fire=np.full((2,2),base), gpp__gpp__VIIRS_Fire=np.full((2,2),base-.4))


class ParallelReportTests(unittest.TestCase):
    def test_prepare_partition_preserves_values_and_rejects_changed_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            first=prepare(root);second=prepare(root)
            self.assertEqual(first,second)
            self.assertEqual(first['primary_regions'],2)
            self.assertEqual([t['kind'] for t in first['tasks']],['component','group','group','science','references'])
            actual=pd.read_parquet(first['components'][0]['annual'])
            expected=report._load_annual([root/'regions/A',root/'regions/B'])
            pd.testing.assert_frame_equal(actual,expected)
            with (root/'regions/A/annual.csv').open('a') as stream:stream.write('\n')
            with self.assertRaisesRegex(ValueError,'identity changed'):prepare(root)

    def test_component_resume_checks_artifacts_and_merged_page_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);prepare(root)
            result=worker(root,0)
            self.assertEqual(result['pages'],3)
            self.assertEqual(len(result['figures']),3)
            self.assertEqual(worker(root,0),result)
            target=destination(root)/'test_merge.pdf'
            self.assertEqual(merge_pdfs([f['pdf'] for f in result['figures']],target),3)
            self.assertEqual(validated_pdf(target),3)
            Path(result['figures'][0]['png']).write_bytes(b'corrupt-test-fixture')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):worker(root,0)

    def test_group_atlas_keeps_separate_group_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);manifest=prepare(root)
            result=worker(root,1)
            self.assertEqual(result['group_type'],'pyrome')
            self.assertEqual(result['group'],'FIL')
            self.assertEqual(result['pages'],1)
            values=pd.read_csv(Path(result['pdf']).parent/'paired_group_values.csv.gz')
            self.assertEqual(set(values['group']),{'FIL'})
            self.assertEqual(set(values['year']),{2015,2016})

    def test_legacy_reuse_requires_equal_exact_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);manifest=prepare(root)
            item=manifest['components'][0];frame=pd.read_parquet(item['annual'])
            legacy=root/'report';legacy.mkdir()
            p=legacy/('paired_'+item['token']+'.csv')
            values=report._paired(frame);values.to_csv(p,index=False)
            self.assertTrue(_legacy_pair_matches(legacy,item,frame))
            values.loc[0,'No_Fire']+=1;values.to_csv(p,index=False)
            self.assertFalse(_legacy_pair_matches(legacy,item,frame))

    def test_main_selection_uses_topic_not_effect_size(self):
        figures=[dict(kind='scenario_summary',variable='gpp',component='gpp'),
                 dict(kind='scenario_summary',variable='TWS',component='soil_7'),
                 dict(kind='native_reference_comparison',variable='gpp',source='common_annual_gpp_pairs.csv.gz')]
        self.assertEqual(select_main_figures([{'figures':figures}]),[figures[0],figures[2]])

    def test_merge_requires_every_worker_and_keeps_group_links(self):
        from matplotlib.backends.backend_pdf import PdfPages
        def appendix(prepared,out):
            p=out/'synthetic.pdf'
            with PdfPages(p) as pdf:report._text_page(pdf,'Synthetic appendix',['Only test evidence.'])
            return dict(figures=[],pdf=str(p))
        def reference(prepared,out):
            pd.DataFrame([dict(reference_product='SYNTHETIC',variable='gpp',scenario='VIIRS_Fire',bias=1.,rmse=2.,units='g C m-2',n=4)]).to_csv(out/'reference_metrics_all_pairs.csv',index=False)
            return appendix(prepared,out)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root);prepared=prepare(root)
            with self.assertRaisesRegex(ValueError,'incomplete'):merge(root)
            with patch('result_analysis.parallel_report.science_task',side_effect=appendix),patch('result_analysis.parallel_report.reference_task',side_effect=reference):
                for task in prepared['tasks']:worker(root,task['index'])
            result=merge(root)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['component_count'],1)
            self.assertEqual(result['component_appendix_pages'],3)
            self.assertEqual(validated_pdf(destination(root)/'REPORT_EN.pdf'),result['main_pages'])
            text=(destination(root)/'REPORT_EN.md').read_text()
            self.assertIn('pyrome / FIL',text)
            self.assertIn('vegetation / forest',text)


if __name__=='__main__':unittest.main()
