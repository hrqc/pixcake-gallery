# -*- coding: utf-8 -*-
"""多租户核心: 摄影师 / 卡密绑定 / 额度 / 归属隔离."""
import os
import sqlite3
import tempfile
import time
import unittest

import db


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.old_file = db.DB_FILE
        self.db_path = os.path.join(self.folder.name, 'test.db')
        db.DB_FILE = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_file
        self.folder.cleanup()

    def _new_card(self, tenant=None, quota=0, days=30, bind=0):
        keys = db.create_license_keys('测试卡', days, quota, bind, 1, 'ut')
        key = keys[0]
        db.activate_license(key, 'MACHINE-UT')
        if tenant:
            db.bind_card_tenant(key, tenant)
        return key

    def test_create_and_find_photographer(self):
        pg = db.create_photographer('张三', 'FP-A')
        self.assertTrue(pg['id'].startswith('p_'))
        self.assertEqual(db.get_photographer(pg['id'])['machine_fp'], 'FP-A')
        self.assertEqual(db.find_photographer_by_machine('FP-A')['id'], pg['id'])
        self.assertEqual(db.find_photographer_by_machine('FP-MISSING'), None)

    def test_card_tenant_binding_and_active_card(self):
        pg = db.create_photographer('李四', 'FP-B')
        key = self._new_card(tenant=pg['id'])
        card = db.tenant_active_card(pg['id'])
        self.assertIsNotNone(card)
        self.assertEqual(card['key'], key)

    def test_consume_quota_enforces_limit(self):
        pg = db.create_photographer('王五', 'FP-C')
        db.create_license_keys('张数卡', 0, 3, 1, 1, 'ut')  # 占位避免 id 冲突
        key = self._new_card(tenant=pg['id'], quota=3, days=0)
        ok, remaining = db.consume_quota(pg['id'], 1)
        self.assertTrue(ok); self.assertEqual(remaining, 2)
        ok, remaining = db.consume_quota(pg['id'], 2)
        self.assertTrue(ok); self.assertEqual(remaining, 0)
        ok, reason = db.consume_quota(pg['id'], 1)
        self.assertFalse(ok); self.assertEqual(reason, 'quota')

    def test_consume_quota_unlimited_when_quota_zero(self):
        pg = db.create_photographer('赵六', 'FP-D')
        self._new_card(tenant=pg['id'], quota=0)
        ok, remaining = db.consume_quota(pg['id'], 100)
        self.assertTrue(ok)
        self.assertEqual(remaining, -1)  # 不限

    def test_consume_quota_no_card(self):
        pg = db.create_photographer('孙七', 'FP-E')
        ok, reason = db.consume_quota(pg['id'], 1)
        self.assertFalse(ok)
        self.assertEqual(reason, 'no_card')

    def test_project_owner_isolation(self):
        pg_a = db.create_photographer('A', 'FP-A')
        pg_b = db.create_photographer('B', 'FP-B')
        db.upsert_project({'id': 'proj-a', 'path': '/x'}, owner=pg_a['id'])
        db.upsert_project({'id': 'proj-b', 'path': '/y'}, owner=pg_b['id'])
        self.assertEqual({p['id'] for p in db.list_projects_for(pg_a['id'])}, {'proj-a'})
        self.assertEqual({p['id'] for p in db.list_projects_for(pg_b['id'])}, {'proj-b'})
        self.assertEqual(db.project_owner('proj-a'), pg_a['id'])
        self.assertEqual(db.project_owner('proj-b'), pg_b['id'])


if __name__ == '__main__':
    unittest.main()
