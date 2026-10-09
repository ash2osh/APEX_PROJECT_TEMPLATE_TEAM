"""Real catalog inventories must distinguish a parent's partition subobjects."""
import re
import sqlite3
import unittest
from pathlib import Path

from scripts.schema_catalog import CatalogError, ObjectKey, _inventory_signature, _object_rows

ROOT = Path(__file__).resolve().parents[1]


class CatalogPartitionTests(unittest.TestCase):
    def production_rows(self):
        source = (ROOT / 'scripts/schema_catalog.sql').read_text(encoding='utf-8')
        query = re.search(r'FOR item IN \(\s*(SELECT .*?)\s*\) LOOP', source, re.S).group(1)
        query = query.replace('c_target_schema', ':owner')
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory = sqlite3.Row
            connection.create_function('TO_CHAR', 2, lambda value, fmt: value)
            connection.executescript('''
              CREATE TABLE all_objects(owner, object_name, object_type, subobject_name,
                status, object_id, data_object_id, timestamp, last_ddl_time);
              CREATE TABLE all_tab_identity_cols(owner, sequence_name, table_name);
              INSERT INTO all_objects VALUES
                ('APP_DEV','ATTENDANCE','TABLE',NULL,'VALID',1,1,'t','t'),
                ('APP_DEV','ATTENDANCE','TABLE PARTITION','P1','VALID',2,2,'t','t'),
                ('APP_DEV','ATTENDANCE','TABLE PARTITION','P2','VALID',3,3,'t','t'),
                ('APP_DEV','PK_ATTENDANCE','INDEX',NULL,'VALID',4,4,'t','t'),
                ('APP_DEV','PK_ATTENDANCE','INDEX PARTITION','P1','VALID',5,5,'t','t'),
                ('APP_DEV','PK_ATTENDANCE','INDEX PARTITION','P2','VALID',6,6,'t','t'),
                ('APP_DEV','ATTENDANCE','TABLE SUBPARTITION','SP1','VALID',7,7,'t','t'),
                ('APP_DEV','ATTENDANCE','TABLE SUBPARTITION','SP2','VALID',8,8,'t','t');
            ''')
            return [{'owner': r['owner'], 'name': r['object_name'], 'type': r['object_type'],
                     'subobject_name': r.get('subobject_name'), 'object_id': r['object_id']}
                    for r in map(dict, connection.execute(query, {'owner': 'APP_DEV'}))]

    def test_production_query_preserves_each_partition_and_parent(self):
        inventory = _object_rows(self.production_rows(), 'APP_DEV')
        self.assertEqual(len(inventory), 8)
        self.assertIn(ObjectKey('APP_DEV', 'ATTENDANCE', 'TABLE'), inventory)
        self.assertEqual(sorted(key.subobject_name for key in inventory if key.object_type == 'TABLE PARTITION'), ['P1', 'P2'])
        self.assertEqual(sorted(key.subobject_name for key in inventory if key.object_type == 'INDEX PARTITION'), ['P1', 'P2'])
        self.assertEqual(sorted(key.subobject_name for key in inventory if key.object_type == 'TABLE SUBPARTITION'), ['SP1', 'SP2'])

    def test_partition_rename_changes_drift_signature_without_changing_object_id(self):
        row = {'owner': 'APP_DEV', 'name': 'ATTENDANCE', 'type': 'TABLE PARTITION', 'subobject_name': 'P1', 'object_id': 2}
        before = _object_rows([row], 'APP_DEV')
        after = _object_rows([{**row, 'subobject_name': 'P2'}], 'APP_DEV')
        self.assertNotEqual(_inventory_signature(before), _inventory_signature(after))

    def test_duplicate_parent_and_duplicate_partition_still_refuse(self):
        for row in ({'owner': 'APP_DEV', 'name': 'T', 'type': 'TABLE'},
                    {'owner': 'APP_DEV', 'name': 'T', 'type': 'TABLE PARTITION', 'subobject_name': 'P1'}):
            with self.subTest(row=row), self.assertRaises(CatalogError):
                _object_rows([row, dict(row)], 'APP_DEV')

    def test_malformed_subobject_names_refuse(self):
        for value in ('', 1, False, []):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                _object_rows([{'owner': 'APP_DEV', 'name': 'T', 'type': 'TABLE PARTITION', 'subobject_name': value}], 'APP_DEV')


if __name__ == '__main__':
    unittest.main()
