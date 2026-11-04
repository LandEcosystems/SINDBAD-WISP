import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import contextmanager
import campaign as c


class CampaignTests(unittest.TestCase):
    def test_keeps_active_and_selects_only_accepted_in_existing_order(self):
        self.assertEqual(c.token_order('C', {'C': {}, 'A': {}, 'D': {}}, ['A', 'B', 'C', 'D'], [], 2), ['C', 'A', 'D'])

    def test_excludes_completed_and_does_not_silently_reduce_count(self):
        with self.assertRaises(ValueError):
            c.token_order('C', {'C': {}, 'A': {}, 'D': {}}, ['A', 'C', 'D'], ['A'], 2)

    def test_zero_additional_rejected(self):
        with self.assertRaises(ValueError):
            c.token_order('C', {'C': {}}, ['C'], [], 0)

    def test_sixteen_complete_stops_instead_of_admitting_17th(self):
        tokens = ['T%06d' % i for i in range(16)]
        completed = []
        for i, token in enumerate(tokens):
            completed, following = c.next_token({'phase': 'CLEANED', 'active_cube': token, 'completed_cubes': completed}, tokens)
            self.assertEqual(following, tokens[i+1] if i < 15 else None)
        self.assertEqual(completed, tokens)

    def test_never_advance_before_cleanup(self):
        for phase in ['BUILD', 'INPUT_VERIFY', 'PREDICT', 'MERGE', 'CLEANUP_WAIT']:
            with self.assertRaises(ValueError):
                c.next_token({'phase': phase, 'active_cube': 'A'}, ['A', 'B'])

    def test_no_foreign_or_skipped_cube(self):
        for state in [{'phase': 'CLEANED', 'active_cube': 'X'},
                      {'phase': 'CLEANED', 'active_cube': 'B', 'completed_cubes': []},
                      {'phase': 'CLEANED', 'active_cube': 'A', 'completed_cubes': ['X']}]:
            with self.assertRaises(ValueError):
                c.next_token(state, ['A', 'B'])

    def test_resume_admission_after_capacity_failure_does_not_duplicate(self):
        self.assertEqual(c.next_token({'phase': 'CLEANED', 'active_cube': 'A', 'completed_cubes': ['A']}, ['A', 'B']), (['A'], 'B'))

    def test_cube_acceptance_and_content_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = root/'cube.nc'; p.write_bytes(b'original bytes')
            ap = root/'acceptance.json'; a = {'passed': True, 'errors': [], 'file_sha256': c.sha(p), 'path': str(p), 'bytes': p.stat().st_size}
            c.atomic(ap, a)
            rec = {'cube_path': str(p), 'cube_stat': {'bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns},
                   'acceptance_path': str(ap), 'cube_acceptance_sha256': c.sha(ap), 'cube_sha256': c.sha(p)}
            c.check_cube(rec, full_hash=True)
            a['passed'] = False; c.atomic(ap, a)
            with self.assertRaises(ValueError): c.check_cube(rec)
            rec['cube_acceptance_sha256'] = c.sha(ap)
            with self.assertRaises(ValueError): c.check_cube(rec)
            a['passed'] = True; c.atomic(ap, a); rec['cube_acceptance_sha256'] = c.sha(ap)
            p.write_bytes(b'changed content')
            with self.assertRaises(ValueError): c.check_cube(rec, full_hash=True)

    def test_bound_manifest_engine_and_checksum_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path=root/'campaign/manifest.json'
            mapping=[('code/release.json','engine_release_sha256'),('manifests/deployment.json','scientific_deployment_sha256'),('manifests/cube_order.json','original_order_sha256')]
            m={'run_root':str(root.resolve()),'tokens':['A','B'],'additional_count':1,'initial_cube':'A','records':[{'token':'A'},{'token':'B'}]}
            for rel,key in mapping:
                c.atomic(root/rel, {'frozen':rel}); m[key]=c.sha(root/rel)
            c.atomic(path,m);path.with_suffix('.sha256').write_text(c.sha(path))
            self.assertEqual(c.load_manifest(root,path),m)
            c.atomic(root/'code/release.json',{'changed':True})
            with self.assertRaises(ValueError):c.load_manifest(root,path)


    def test_real_controller_hook_stops_after_authorized_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); manifest=root/'campaign/manifest.json'; tokens=['T%06d'%i for i in range(16)]
            m={'run_root':str(root.resolve()),'tokens':tokens,'additional_count':15,'initial_cube':tokens[0],
               'records':[{'token':t} for t in tokens]}
            for rel,key in [('code/release.json','engine_release_sha256'),('manifests/deployment.json','scientific_deployment_sha256'),('manifests/cube_order.json','original_order_sha256')]:
                c.atomic(root/rel,{'fixed':rel});m[key]=c.sha(root/rel)
            c.atomic(root/'sampling/named_production_regions.json',{'fixed':'catalog'})
            m['catalog_sha256']=c.sha(root/'sampling/named_production_regions.json')
            c.atomic(manifest,m); manifest.with_suffix('.sha256').write_text(c.sha(manifest))
            c.atomic(manifest.parent/'campaign_code.json',{'files':{}})
            state={'phase':'CLEANED','active_cube':tokens[0],'completed_cubes':[],
                   'campaign_manifest_sha256':c.sha(manifest)}
            class Queue:
                def __init__(self,*args):pass
                def snapshot(self):return copy.deepcopy(state)
                @contextmanager
                def edit(self):yield state
            admitted=[]
            def admit(config,token):
                admitted.append(token); state.update(active_cube=token,phase='CLEANED'); return {'passed':True}
            migration=SimpleNamespace(admit_cube=admit)
            ctl=SimpleNamespace(submit=lambda *args:None)
            def control(config):
                for i in range(16):migration.activate_next(config)
                self.assertEqual(state['phase'],'COMPLETE')
            ctl.control=control
            with patch.object(c,'engine',return_value=(SimpleNamespace(Queue=Queue),ctl,migration,{'batch_root':str(root)})), patch.object(c,'check_cube') as check, patch('sys.argv',['campaign.py','control','--run-root',str(root),'--manifest',str(manifest)]):
                c.main()
                self.assertEqual(check.call_count,15)
            self.assertEqual(admitted,tokens[1:]);self.assertEqual(state['completed_cubes'],tokens)
            self.assertTrue(c.read(manifest.parent/'complete.json')['passed'])


if __name__ == '__main__':
    unittest.main()
