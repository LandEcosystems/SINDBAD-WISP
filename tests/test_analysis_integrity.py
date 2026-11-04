import json
from pathlib import Path
import tempfile
import unittest
from result_analysis.integrity import verify_region_artifacts
from result_analysis.io import file_hash


class ArtifactIntegrityTests(unittest.TestCase):
    def test_modified_output_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data=root/'annual.csv'; data.write_text('value\n1\n')
            (root/'completion.json').write_text(json.dumps({'artifacts':['annual.csv'], 'checksums':{'annual.csv':file_hash(data)}}))
            self.assertEqual(verify_region_artifacts(root)['verified_artifacts'],1)
            data.write_text('value\n2\n')
            with self.assertRaisesRegex(RuntimeError,'modified'): verify_region_artifacts(root)

    def test_missing_receipt_is_not_a_verified_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'completion.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'Incomplete'): verify_region_artifacts(root)

    def test_external_artifact_path_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'completion.json').write_text(json.dumps({'artifacts':['../other'], 'checksums':{'../other':'irrelevant'}}))
            with self.assertRaisesRegex(RuntimeError,'direct region output'): verify_region_artifacts(root)
