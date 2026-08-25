# -*- coding: utf-8 -*-
"""管理端平台控制台端到端:
中央服务器 A (真实数据) + 管理端本地实例 B (平台代理).
验证: 连接配置 -> 代理拉摄影师 -> 代理加张数 -> 代理拉全平台订单."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GALLERY_DIR = os.path.dirname(HERE)
sys.path.insert(0, GALLERY_DIR)

import db
import gallery


def new_client(port):
    def req(method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        hdrs = {'Content-Type': 'application/json'} if body is not None else {}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers=hdrs)
        r = c.getresponse(); data = r.read().decode('utf-8'); c.close()
        return r.status, json.loads(data) if data else {}
    return req


def main():
    tmp = tempfile.mkdtemp(prefix='pixcake-platform-e2e-')
    adata = os.path.join(tmp, 'a-data')
    bdata = os.path.join(tmp, 'b-data')
    ws = os.path.join(tmp, 'ws')
    os.makedirs(adata); os.makedirs(bdata); os.makedirs(ws)

    print('[1/5] 中央服务器 A 数据: 摄影师 + 张数卡 + 订单')
    old_db = db.DB_FILE
    db.DB_FILE = os.path.join(adata, 'gallery.db')
    gallery.DATA_DIR = adata
    gallery.TENANT_ROOT = os.path.join(adata, 'tenants')
    gallery.CLEAN_CACHE_DIR = os.path.join(adata, 'cache')
    db.init_db()

    pg = db.create_photographer('测试摄影师', 'FP-A')
    key = db.create_license_keys('张数卡', 0, 3, 1, 1, 'plat')[0]
    db.activate_license(key, 'FP-A')
    db.bind_card_tenant(key, pg['id'])
    db.upsert_project({'id': 'proj-1', 'name': '测试相册', 'path': '/x'}, owner=pg['id'])
    db.ensure_delivery('proj-1')
    db.create_order('proj-1', 'session1234', ['p1', 'p2'], 10, 0, 20, 20, '客户A', '')

    print('[2/5] 启动中央服务器 A')
    srv_a = gallery.GalleryServer(('127.0.0.1', 0), ws, 'tok-A', wm_workers=1)
    ta = threading.Thread(target=srv_a.serve_forever, daemon=True); ta.start()
    a_port = srv_a.server_port

    print('[3/5] 启动管理端本地实例 B (--data-dir)')
    env = dict(os.environ); env['PYTHONIOENCODING'] = 'utf-8'
    b_port = 9711
    proc = subprocess.Popen(
        [sys.executable, os.path.join(GALLERY_DIR, 'gallery.py'),
         '--port', str(b_port), '--ws', ws, '--token', 'tok-B',
         '--data-dir', bdata],
        cwd=GALLERY_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    b = new_client(b_port)

    try:
        print('[4/5] 管理端连接平台 + 代理调用')
        # 未配置时代理被拦
        s, j = b('GET', '/api/platform/proxy?path=/api/admin/photographers')
        assert s == 400 and '未配置' in j.get('error', ''), (s, j)
        # 连接
        s, j = b('POST', '/api/platform/config',
                 {'server': 'http://127.0.0.1:%d' % a_port, 'token': 'tok-A'})
        assert s == 200 and j.get('ok'), (s, j)
        s, j = b('GET', '/api/platform/status')
        assert j['configured'] is True and j['server'], j
        # 错误 token 被拒
        s, j = b('POST', '/api/platform/config',
                 {'server': 'http://127.0.0.1:%d' % a_port, 'token': 'BAD'})
        assert s == 400, (s, j)
        # 代理拉摄影师
        s, j = b('GET', '/api/platform/proxy?path=/api/admin/photographers')
        assert s == 200 and j['photographers'][0]['id'] == pg['id'], (s, j)
        # 代理加张数
        s, j = b('POST', '/api/platform/proxy',
                 {'path': '/api/license/admin/quota', 'body': {'key': key, 'delta': 5}})
        assert s == 200 and j['quota'] == 8, (s, j)
        quota_now = j['quota']
        # 代理拉全平台订单
        s, j = b('GET', '/api/platform/proxy?path=/api/admin/orders')
        assert s == 200 and j['orders'][0]['owner'] == pg['id'], (s, j)
        assert j['orders'][0]['count'] == 2 and j['orders'][0]['status'] == 'submitted', j
        print('      代理: 摄影师 %s, 加张后 quota=%d, 订单 %d 条'
              % (pg['id'], quota_now, len(j['orders'])))

        print('[5/5] 平台控制台页面')
        c = http.client.HTTPConnection('127.0.0.1', b_port, timeout=10)
        c.request('GET', '/platform.html')
        r = c.getresponse(); html = r.read().decode('utf-8'); c.close()
        assert r.status == 200 and '平台控制台' in html and '摄影师管理' in html, r.status
        print('      平台页可访问, %d bytes' % len(html))

        print('\n===== 管理端平台控制台端到端全通过 =====')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv_a.shutdown(); srv_a.server_close(); ta.join(timeout=2)
        srv_a.export_manager.shutdown(); srv_a.image_service.shutdown()
        db.DB_FILE = old_db
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
