# -*- coding: utf-8 -*-
"""自动发现 + 全量预热: 后台线程定时扫描整个像素蛋糕工作区,
任何相册的新照片 / 重精修都会自动入库并预生成去水印图 (定价后置, 预热不依赖交付配置).

用法: 在 gallery.main() 里创建 Warmer(ws_root, image_service) 并 start().
注意: 本模块只依赖 db/scanner/image_service, 不 import gallery,
避免 gallery 以 __main__ 运行时被本模块二次 import (触发顶层代码重复执行).
"""
from __future__ import annotations

import threading
import time

import db
import scanner


def _rescan_workspace(ws_root):
    """全工作区扫描: 发现新相册 + 同步每个相册的照片 (仅 listdir/stat, 很快)."""
    albums = scanner.find_albums(ws_root)
    for a in albums:
        db.upsert_project(a)
        photos = scanner.scan_project_photos(a['path'])
        disk = []
        for ph in photos:
            ph['project_id'] = a['id']
            ph['key'] = '%s|%s' % (a['id'], ph['photo_id'])
            disk.append(ph)
        db.sync_photos(a['id'], disk)
    return albums


class Warmer:
    def __init__(self, ws_root, image_service, interval=30, batch=4):
        self.ws_root = ws_root
        self.image_service = image_service
        self.interval = max(5, int(interval))
        self.batch = max(1, int(batch))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            'running': False,
            'phase': 'idle',
            'last_scan': 0,
            'cycle_started': 0,
            'total': 0,
            'built': 0,          # 本次真正重建的图数
            'cached': 0,         # 命中缓存的图数
            'failed': 0,
            'failures': [],
            'last_error': '',
        }
        self._thread = None

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='prewarm', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    # ---------------- 主循环 ----------------
    def _loop(self):
        self._set(running=True)
        try:
            while not self._stop.is_set():
                try:
                    self._cycle()
                except Exception as exc:
                    self._set(phase='error', last_error=str(exc))
                self._stop.wait(self.interval)
        finally:
            self._set(running=False, phase='idle')

    def _cycle(self):
        """一轮: 扫描发现新相册/新精修 -> 对无缓存或指纹变化的照片批量预生成."""
        try:
            _rescan_workspace(self.ws_root)
        except Exception as exc:
            self._set(last_error='扫描失败: %s' % exc)
            return
        self._set(last_scan=time.time())
        photos = db.list_all_photos()
        self._set(phase='prewarm', cycle_started=time.time(),
                  total=len(photos), built=0, cached=0, failed=0, failures=[],
                  last_error='')

        built = cached = failed = 0
        failures = []
        idx = 0
        while idx < len(photos):
            if self._stop.is_set():
                return
            batch = photos[idx:idx + self.batch]
            idx += self.batch
            futures = {}
            for photo in batch:
                was_cached = self.image_service.cached(photo)
                futures[photo['key']] = (photo, was_cached,
                                         self.image_service.request(photo))
            for _key, (photo, was_cached, future) in futures.items():
                try:
                    future.result(timeout=300)
                    if was_cached:
                        cached += 1
                    else:
                        built += 1
                except Exception as exc:
                    failed += 1
                    failures.append({
                        'photo_id': photo.get('photo_id'),
                        'error': str(exc)[:100],
                    })
            self._set(phase='prewarm', built=built, cached=cached, failed=failed,
                      failures=failures[-50:])
        self._set(phase='idle', built=built, cached=cached, failed=failed,
                  failures=failures[-50:])


class TenantWarmer:
    """多租户版预热: 摄影师上传相册后按需触发去水印 (非定时).
    只处理某摄影师的项目照片, 与主工作区 Warmer 互不干扰."""
    def __init__(self, tenant_root, image_service, batch=2):
        self.tenant_root = tenant_root
        self.image_service = image_service
        self.batch = max(1, int(batch))
        self._lock = threading.Lock()
        self._state = {}

    def trigger(self, slug):
        """请求为某摄影师预热. 若正在跑则记 pending, 结束后自动再跑一轮."""
        with self._lock:
            st = self._state.setdefault(slug, {'running': False, 'pending': False,
                                               'built': 0, 'cached': 0, 'failed': 0,
                                               'photos': 0, 'last': 0})
            if st['running']:
                st['pending'] = True
                return
            st['running'] = True
        threading.Thread(target=self._warm, args=(slug,), daemon=True,
                         name='tenant-warm-%s' % slug).start()

    def status(self, slug=None):
        with self._lock:
            if slug:
                return dict(self._state.get(slug, {}))
            return {k: dict(v) for k, v in self._state.items()}

    def _warm(self, slug):
        try:
            while True:
                photos = db.list_all_photos(owner=slug)
                built = cached = failed = 0
                for i in range(0, len(photos), self.batch):
                    futures = []
                    for photo in photos[i:i + self.batch]:
                        was_cached = self.image_service.cached(photo)
                        futures.append((photo, was_cached, self.image_service.request(photo)))
                    for photo, was_cached, fut in futures:
                        try:
                            fut.result(timeout=600)
                            if was_cached:
                                cached += 1
                            else:
                                built += 1
                        except Exception:
                            failed += 1
                with self._lock:
                    st = self._state[slug]
                    st.update(running=False, built=built, cached=cached,
                              failed=failed, photos=len(photos), last=time.time())
                    if st['pending']:
                        st['pending'] = False
                        st['running'] = True
                        continue   # 上传又来了, 再跑一轮
                    break
        except Exception as exc:
            with self._lock:
                self._state[slug] = {'running': False, 'error': str(exc)}
