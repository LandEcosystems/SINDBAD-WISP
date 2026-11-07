"""A quiescent production upgrade must preserve committed work and resume."""
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import sys

HERE=Path(__file__).resolve().parent
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
ops=load('_backlog_common','common.py')
with patch.dict(sys.modules,common=ops):
    target=load('_backlog_upgrade','backlog_upgrade.py')


class BacklogUpgradeTest(unittest.TestCase):
    def test_production_state_preserved_and_upgrade_repeatable(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'campaign';source=Path(d)/'source';source.mkdir()
            (source/'controller.py').write_text('# new controller\n')
            (root/'code/batch400').mkdir(parents=True)
            (root/'code/batch400/controller.py').write_text('# original controller\n')
            cfg=dict(root=str(root),scratch=str(root/'scratch'),max_lanes=10,
                     region_budget_bytes=256*ops.GIB)
            ops.atomic(root/'config/campaign.json',cfg)
            ops.atomic(root/'manifests/named_production_regions.json',{'regions':['T000001']})
            release=dict(files={'batch400/controller.py':ops.sha(root/'code/batch400/controller.py')},
                         campaign_config_sha256=ops.sha(root/'config/campaign.json'),
                         catalog_sha256=ops.sha(root/'manifests/named_production_regions.json'))
            ops.atomic(root/'code/release.json',release)
            token='T000001';rr=root/'regions'/token
            queue=dict(phase='PREDICT',tasks={'a':dict(dates=['2020-01-01'],input_status='ready',prediction_status='queued')})
            ops.atomic(rr/'state/queue.json',queue)
            ops.atomic(rr/'config/deployment.json',{'checkpoint':'unchanged'})
            ops.atomic(rr/'manifests/identity.json',{'campaign_release_sha256':ops.sha(root/'code/release.json')})
            state=dict(lanes=[token]+[None]*9,lane_limit=10,producer_cap=2,
                       regions={token:dict(status='ACTIVE',selected_days=1)},phase='RUNNING')
            ops.atomic(root/'state/campaign.json',state)
            ops.atomic(root/'state/submissions/123.json',dict(kind='gpu',job='123'))
            def fake_run(args,**kwargs):
                return types.SimpleNamespace(stdout='123|w4e-gpu|PENDING\n' if args[0]=='squeue' else 'JobState=PENDING Reason=JobHeldUser')
            with patch.object(target.subprocess,'run',side_effect=fake_run), \
                 patch.object(target.shutil,'disk_usage',return_value=types.SimpleNamespace(free=8*1024*ops.GIB)):
                target.upgrade(source,root,'abcdef123','123')
                target.upgrade(source,root,'abcdef123','123')
            self.assertEqual(ops.read(rr/'state/queue.json'),queue)
            self.assertEqual(ops.read(rr/'config/deployment.json'),{'checkpoint':'unchanged'})
            after=ops.read(root/'state/campaign.json')
            self.assertEqual(after['regions'],state['regions'])
            self.assertEqual(after['lanes'][:10],state['lanes'])
            self.assertEqual((len(after['lanes']),after['producer_cap']),(20,21))
            self.assertEqual(ops.read(root/'config/campaign.json')['backlog_budget_bytes'],5*1024*ops.GIB)
            ops.verify_release(root)

    def test_running_gpu_prevents_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            ops.atomic(root/'state/submissions/123.json',{'kind':'gpu'})
            with patch.object(target.subprocess,'run',return_value=types.SimpleNamespace(stdout='123|w4e-gpu|RUNNING\n')):
                with self.assertRaisesRegex(ValueError,'must stop'):
                    target.quiescent(root,'123')


if __name__=='__main__':unittest.main()
