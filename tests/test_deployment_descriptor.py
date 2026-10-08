import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.deployment_descriptor import DescriptorError, read_descriptor, validate_descriptor, verify_effective_deployment


def descriptor():
    return {'workspace':{'name':'TEAM'},'app':{'id':100,'databaseSession':{'parsingSchema':'DEMO'}}}


class DeploymentDescriptorTests(unittest.TestCase):
    def test_qualified_cutoff_is_a_valid_timestamp_without_timezone_guessing(self):
        from scripts.deployment_descriptor import validate_descriptor
        data = {'workspace': {'name': 'DEMO'}, 'app': {'id': 100, 'databaseSession': {'parsingSchema': 'DEMO'}, 'sessionStateProtection': {'allowUrlsCreatedAfter': '2026-10-01T00:00:00'}}}
        validate_descriptor(data, 100)
        for invalid in ('2026-02-30T00:00:00', '2026-10-01', '2026-10-01T00:00:00Z', True):
            data['app']['sessionStateProtection']['allowUrlsCreatedAfter'] = invalid
            with self.assertRaises(ValueError): validate_descriptor(data, 100)

    def test_required_explicit_identity(self):
        validate_descriptor(descriptor(),100)
        for key in ('workspace','app'):
            data=descriptor(); del data[key]
            with self.subTest(key=key),self.assertRaises(DescriptorError): validate_descriptor(data,100)

    def test_boolean_string_wrong_and_out_of_range_ids_refuse(self):
        for value in (True,'100',0,-1,101,10**18):
            data=descriptor(); data['app']['id']=value
            with self.subTest(value=value),self.assertRaisesRegex(DescriptorError,'app.id'): validate_descriptor(data,100)

    def test_invalid_workspace_or_schema(self):
        for value in ('','TEAM\nOTHER',None,100):
            data=descriptor();data['workspace']['name']=value
            with self.subTest(value=value),self.assertRaises(DescriptorError):validate_descriptor(data,100)
        for value in ('demo','DEMO;DROP',True,None):
            data=descriptor();data['app']['databaseSession']['parsingSchema']=value
            with self.subTest(value=value),self.assertRaises(DescriptorError):validate_descriptor(data,100)

    def test_unknown_property_reports_full_path(self):
        data=descriptor();data['app']['runtime']={'debuggging':True}
        with self.assertRaisesRegex(DescriptorError,'app.runtime.debuggging'):validate_descriptor(data,100)

    def test_qualified_name_runtime_and_salt_overrides(self):
        data=descriptor();data['app'].update({'name':"O'Neil & أحمد",'runtime':{'debugging':True,'logging':False},
            'sessionStateProtection':{'checksumSalt':'a'*64}})
        validate_descriptor(data,100)

    def test_subscription_mapping_uses_numeric_json_values_and_string_id_keys(self):
        data = descriptor(); data['subscription'] = {'masterApps': {'200': 300, '201': 301}}
        validate_descriptor(data, 100)
        for mapping in ({'0200': 300}, {'0': 300}, {'200': '300'}, {'200': True}, {'200': 0}, {'200': 10**18}, [], None):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(DescriptorError, 'subscription.masterApps'):
                data['subscription']['masterApps'] = mapping; validate_descriptor(data, 100)

    def test_wrong_optional_types(self):
        for property,value in (('name',True),('runtime',[]),('sessionStateProtection',None)):
            data=descriptor();data['app'][property]=value
            with self.subTest(property=property),self.assertRaises(DescriptorError):validate_descriptor(data,100)
        for value in ('Yes',1,None):
            data=descriptor();data['app']['runtime']={'debugging':value}
            with self.subTest(value=value),self.assertRaises(DescriptorError):validate_descriptor(data,100)

    def test_invalid_salt_error_never_contains_salt_value(self):
        for value in ('sensitive-invalid-salt','a'*63,'z'*64,None,100):
            data=descriptor();data['app']['sessionStateProtection']={'checksumSalt':value}
            with self.subTest(value=value),self.assertRaises(DescriptorError) as error:validate_descriptor(data,100)
            if isinstance(value,str):self.assertNotIn(value,str(error.exception))

    def test_read_rejects_bom_duplicate_keys_nonfinite_json_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'dev.json'
            for raw in (b'\xef\xbb\xbf'+json.dumps(descriptor()).encode(),b'{"app":{},"app":{}}',b'{"app":NaN}',b'{'):
                path.write_bytes(raw)
                with self.subTest(raw=raw),self.assertRaises(DescriptorError):read_descriptor(path,100)
            path.write_text(json.dumps(descriptor()))
            link=path.with_name('link.json');link.symlink_to(path)
            with self.assertRaises(DescriptorError):read_descriptor(link,100)

    def test_selected_descriptor_is_never_merged_with_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'dev.json';path.write_text(json.dumps(descriptor()))
            path.with_name('default.json').write_text(json.dumps({'app':{'name':'ignored','runtime':{'debugging':True}}}))
            self.assertEqual(read_descriptor(path,100),descriptor())

    def test_effective_identity_and_selected_values_must_match(self):
        data=descriptor();data['app']['runtime']={'debugging':True,'logging':False}
        verify_effective_deployment(data,copy.deepcopy(data))
        for path,value in ((('workspace','name'),'OTHER'),(('app','id'),101),(('app','databaseSession','parsingSchema'),'OTHER'),(('app','runtime','debugging'),False)):
            observed=copy.deepcopy(data);node=observed
            for part in path[:-1]:node=node[part]
            node[path[-1]]=value
            with self.subTest(path=path),self.assertRaises(DescriptorError):verify_effective_deployment(data,observed)

    def test_generated_export_salt_is_required_when_selected_and_omission_does_not_imply_preservation(self):
        data=descriptor();data['app']['sessionStateProtection']={'checksumSalt':'a'*64}
        observed=descriptor();observed['app']['sessionStateProtection']={'checksumSalt':'A'*64}
        verify_effective_deployment(data,observed)
        observed['app']['sessionStateProtection']['checksumSalt']='b'*64
        with self.assertRaises(DescriptorError) as error:verify_effective_deployment(data,observed)
        self.assertNotIn('b'*64,str(error.exception));self.assertNotIn('a'*64,str(error.exception))
        verify_effective_deployment(descriptor(),observed)
