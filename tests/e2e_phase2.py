# -*- coding: utf-8 -*-
"""Phase 2 本地端到端: 摄影师上传相册 -> 扫描建站 -> 客户选片/下单/确认
-> 导出 -> 下载扣张数 -> 超额阻断 -> 管理员加张数恢复.

跑法:  python tests/e2e_phase2.py
依赖真实工作区里有带原图的相册 (与 test_services 相同的图库样本).
"""
import base64
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import scanner
import gallery
from gallery import (ADMIN_OWNER, tenant_upload_root, tenant_project_id,
                     tenant_ws_root)


def pick_source():
    """从真实主工作区挑一个相册, 含 >=3 张精修+原图都在盘上的照片."""
    for p in db.list_projects():
        hits = [ph for ph in db.list_photos(p['id'])
                if ph['src_3000'] and os.path.isfile(ph['src_3000'])
                and ph['src_o_3000'] and os.path.isfile(ph['src_o_3000'])]
        if len(hits) >= 3:
            return p, hits[:3]
    raise SystemExit('找不到测试源相册 (需要精修+原图齐全的相册)')


def new_client(port):
    jar = {}

    def req(method, path, body=None, headers=None):
        hdrs = dict(headers or {})
        if jar.get('g'):
            hdrs.setdefault('Cookie', 'g=%s' % jar['g'])
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
        conn.request(method, path, body=body, headers=hdrs)
        r = conn.getresponse()
        data = r.read()
        raw_hdrs = dict(r.getheaders())
        conn.close()
        for k, v in raw_hdrs.items():
            if k.lower() == 'set-cookie' and v.startswith('g='):
                jar['g'] = v.split(';')[0][2:]
        return r.status, data

    def post_json(path, payload):
        return req('POST', path, json.dumps(payload).encode('utf-8'),
                   {'Content-Type': 'application/json'})

    return req, post_json, jar


def main():
    tmp = tempfile.mkdtemp(prefix='pixcake-e2e2-')
    print('[1/9] 准备临时环境', tmp)
    src_album, src_photos = pick_source()
    print('      源相册 %s, 取 %d 张' % (src_album['id'], len(src_photos)))

    # 换成临时数据目录 (先读真实库挑源, 再切换)
    old_db = db.DB_FILE
    db.DB_FILE = os.path.join(tmp, 'gallery.db')
    gallery.DATA_DIR = tmp
    gallery.TENANT_ROOT = os.path.join(tmp, 'tenants')
    gallery.CLEAN_CACHE_DIR = os.path.join(tmp, 'cache')
    db.init_db()

    try:
        # [2] 建摄影师 + 3 张额度的张数卡
        print('[2/9] 建摄影师 + 张数卡(3张)')
        pg = db.create_photographer('端到端测试', 'E2E-FP')
        key = db.create_license_keys('张数卡', 0, 3, 1, 1, 'e2e')[0]
        db.activate_license(key, 'E2E-FP')
        db.bind_card_tenant(key, pg['id'])
        slug = pg['id']
        print('      slug=%s key=%s' % (slug, key))

        # [3] 把源相册的 thumbnail_cache 复制进租户上传工作区
        print('[3/9] 复制源相册到租户上传工作区')
        rel = os.path.relpath(src_album['path'], scanner.DEFAULT_WS)
        dest = os.path.join(tenant_upload_root(slug), rel)
        os.makedirs(dest, exist_ok=True)
        shutil.copytree(os.path.join(src_album['path'], 'thumbnail_cache'),
                        os.path.join(dest, 'thumbnail_cache'))
        album_id = rel.replace(os.sep, '_')  # user_album

        # [4] 启动 GalleryServer (空主工作区, 只跑租户流)
        print('[4/9] 启动服务器')
        srv = gallery.GalleryServer(('127.0.0.1', 0), os.path.join(tmp, 'ws'),
                                    'admin-token', wm_workers=1)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_port
        req, post_json, jar = new_client(port)

        try:
            # [5] 租户登录 -> 上传(成功+越权拦截) -> 扫描建站
            print('[5/9] 租户登录 + 上传 + 扫描')
            status, data = post_json('/api/tenant/login', {'key': key})
            assert status == 200, (status, data)
            assert jar.get('g'), '登录未拿到会话 cookie'
            status, data = req('GET', '/api/tenant/me')
            me = json.loads(data)
            assert me['tenant']['id'] == slug, me
            print('      登录 ok, slug=%s' % me['tenant']['id'])

            status, data = post_json('/api/tenant/upload', {
                'rel_path': 'probe/hello.txt', 'data_b64': base64.b64encode(b'hello').decode()})
            assert status == 200 and json.loads(data)['ok'], (status, data)
            probe_path = os.path.join(tenant_upload_root(slug), 'probe', 'hello.txt')
            assert os.path.isfile(probe_path) and open(probe_path, 'rb').read() == b'hello', '上传文件未落盘'
            # 路径穿越拦截
            status, data = post_json('/api/tenant/upload', {
                'rel_path': '../../evil.txt', 'data_b64': base64.b64encode(b'x').decode()})
            assert status == 400, (status, data)
            print('      上传 ok, 越权路径被拦截')

            status, data = post_json('/api/tenant/scan', {})
            assert status == 200, (status, data)
            projs = json.loads(data)['projects']
            pid = tenant_project_id(slug, album_id)
            assert pid in {p['id'] for p in projs}, (projs, pid)
            assert db.project_owner(pid) == slug
            print('      扫描建站 ok, project=%s' % pid)

            # [6] 等预热去水印完成
            print('[6/9] 等待租户预热去水印')
            deadline = time.time() + 120
            done = False
            while time.time() < deadline:
                status, data = req('GET', '/api/tenant/prewarm')
                pw = json.loads(data).get('prewarm', {})
                if not pw.get('running') and pw.get('error') is None and \
                        pw.get('photos', 0) >= len(src_photos):
                    done = True
                    break
                time.sleep(1)
            assert done, '预热超时'
            assert pw.get('failed', 0) == 0, pw
            print('      预热 ok: built=%d cached=%d photos=%d' %
                  (pw.get('built', 0), pw.get('cached', 0), pw.get('photos', 0)))

            # [7] 交付配置 + 客户选片/下单/摄影师确认
            print('[7/9] 交付配置 + 客户下单 + 确认')
            status, data = post_json('/api/tenant/delivery', {
                'p': pid, 'title': 'E2E 交付', 'price': 0, 'free_count': 0,
                'enabled': 1})
            assert status == 200, (status, data)
            code = db.get_delivery(pid)['code']
            session = 'e2ecustomer1'
            status, data = post_json('/api/delivery/select', {
                'k': code, 'session': session,
                'items': [{'photo_id': ph['photo_id'], 'selected': True}
                          for ph in src_photos]})
            assert status == 200, (status, data)
            status, data = post_json('/api/delivery/checkout',
                                     {'k': code, 'session': session,
                                      'customer_name': '客户A'})
            assert status == 200, (status, data)
            order = json.loads(data)['order']
            assert order['status'] == 'submitted', order
            assert order['count'] == len(src_photos), order
            status, data = post_json('/api/tenant/delivery/confirm',
                                     {'order_id': order['id'], 'agree': True})
            assert status == 200, (status, data)
            o = json.loads(data)['order']
            assert o['status'] == 'confirmed', o
            print('      订单 %d 确认 ok, %d 张' % (order['id'], o['count']))

            # [8] 导出 -> 下载 -> 扣张数
            print('[8/9] 导出 + 下载扣张数')
            status, data = post_json('/api/delivery/export',
                                     {'k': code, 'session': session,
                                      'order_id': order['id']})
            assert status == 202, (status, data)
            job = json.loads(data)
            job_id = job['id']
            deadline = time.time() + 120
            while time.time() < deadline:
                status, data = req('GET', '/api/delivery/export_status?k=%s&s=%s&order_id=%d&id=%s'
                                   % (code, session, order['id'], job_id))
                st = json.loads(data)
                if st['status'] in ('ready', 'failed'):
                    break
                time.sleep(1)
            assert st['status'] == 'ready', st
            card = db.tenant_active_card(slug)
            used_before = card['quota_used'] or 0
            status, data = req('GET', '/api/delivery/download?k=%s&s=%s&order_id=%d&job_id=%s'
                               % (code, session, order['id'], job_id))
            assert status == 200 and data[:2] == b'PK', (status, data[:10])
            card = db.tenant_active_card(slug)
            used_after = card['quota_used'] or 0
            assert used_after == used_before + len(src_photos), \
                (used_before, used_after, len(src_photos))
            print('      下载 ok, quota_used %d -> %d' % (used_before, used_after))

            # [9] 超额阻断 + 管理员加张数恢复
            print('[9/9] 超额阻断 -> 加张数恢复')
            session2 = 'e2ecustomer2'
            status, data = post_json('/api/delivery/select', {
                'k': code, 'session': session2,
                'items': [{'photo_id': ph['photo_id'], 'selected': True}
                          for ph in src_photos]})
            assert status == 200, (status, data)
            status, data = post_json('/api/delivery/checkout',
                                     {'k': code, 'session': session2, 'customer_name': '客户B'})
            order2 = json.loads(data)['order']
            status, data = post_json('/api/tenant/delivery/confirm',
                                     {'order_id': order2['id'], 'agree': True})
            assert json.loads(data)['order']['status'] == 'confirmed'
            status, data = post_json('/api/delivery/export',
                                     {'k': code, 'session': session2, 'order_id': order2['id']})
            assert status == 402, (status, data)  # 预检: 3+3 > 3 被拦
            print('      超额导出被拦 (402): %s' % json.loads(data).get('error'))

            db.add_quota_to_key(key, 3)          # 管理员给摄影师加 3 张
            print('      管理员加 3 张, 恢复:')
            status, data = post_json('/api/delivery/export',
                                     {'k': code, 'session': session2, 'order_id': order2['id']})
            assert status == 202, (status, data)
            print('      恢复导出 ok')

            print('\n===== Phase 2 端到端全通过 =====')
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
            srv.export_manager.shutdown()
            srv.image_service.shutdown()
    finally:
        db.DB_FILE = old_db
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
