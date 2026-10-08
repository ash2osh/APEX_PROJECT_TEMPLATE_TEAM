import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.deployment_descriptor import DescriptorError, observe_deployment, source_projection


class VerifyDeploymentTests(unittest.TestCase):
    def test_public_identity_runtime_and_generated_salt_are_independent_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            expected={'workspace':{'name':'TEAM'},'app':{'id':100,'name':'Selected',
                'databaseSession':{'parsingSchema':'DEMO'},'runtime':{'debugging':True,'logging':False},
                'sessionStateProtection':{'checksumSalt':'a'*64}}}
            public=copy.deepcopy(expected);del public['app']['sessionStateProtection']
            (root/'public.json').write_text(json.dumps(public))
            (root/'default.json').write_text(json.dumps({'app':{'id':100,'sessionStateProtection':{'checksumSalt':'a'*64}}}))
            observe_deployment(root/'public.json',root/'default.json',expected)
            public['app']['runtime']['debugging']=False
            (root/'public.json').write_text(json.dumps(public))
            with self.assertRaisesRegex(DescriptorError,'app.runtime.debugging'):
                observe_deployment(root/'public.json',root/'default.json',expected)

    def test_missing_effective_evidence_refuses(self):
        with self.assertRaises(DescriptorError):
            observe_deployment(Path('/missing/public.json'),Path('/missing/default.json'),{'app':{}})

    def test_only_selected_top_level_name_can_differ(self):
        expected={'app':{'name':'New'}}
        before=b'app SAMPLE (\n    name: Old\n    runtime {\n        logging: true\n    }\n)\n'
        after=before.replace(b'name: Old',b'name: New')
        self.assertEqual(source_projection(before,expected),source_projection(after,expected))
        self.assertNotEqual(source_projection(before,expected),source_projection(after.replace(b'logging: true',b'logging: false'),expected))
        self.assertNotEqual(source_projection(before,{'app':{}}),source_projection(after,{'app':{}}))

    def test_ambiguous_name_projection_fails_closed(self):
        for raw in (b'app SAMPLE ()\n',b'    name: One\n    name: Two\n'):
            with self.assertRaises(DescriptorError):source_projection(raw,{'app':{'name':'New'}})

    def test_subscription_remap_requires_generated_and_public_destination_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = {'workspace': {'name': 'DEMO'}, 'app': {'id': 100, 'databaseSession': {'parsingSchema': 'DEMO'}}, 'subscription': {'masterApps': {'200': 300}}}
            public = copy.deepcopy(expected); public['subscription'] = {'masterApplicationIds': [300]}
            generated = {'subscription': {'masterApps': {'300': 300}}}
            (root / 'public.json').write_text(json.dumps(public)); (root / 'default.json').write_text(json.dumps(generated))
            observe_deployment(root / 'public.json', root / 'default.json', expected)
            public['subscription']['masterApplicationIds'] = [200]
            (root / 'public.json').write_text(json.dumps(public))
            with self.assertRaisesRegex(DescriptorError, 'subscription'):
                observe_deployment(root / 'public.json', root / 'default.json', expected)

    def test_only_structural_subscription_master_is_remapped(self):
        from scripts.deployment_descriptor import remap_subscription_source
        expected = {'subscription': {'masterApps': {'200': 300}}}
        raw = b'list sample (\n    name: Sample\n    subscription {\n        master: @/200/sample\n    }\n    comments: @/200/sample\n)\n'
        transformed = remap_subscription_source(raw, expected)
        self.assertEqual(transformed, raw.replace(b'master: @/200/sample', b'master: @/300/sample'))
        code = b'process code (\n    plsqlCode: ```\n    subscription {\n        master: @/200/sample\n    }\n```\n)\n'
        self.assertEqual(remap_subscription_source(code, expected), code)
