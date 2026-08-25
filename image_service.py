# -*- coding: utf-8 -*-
"""FXIP decode, watermark removal, and versioned clean-image cache."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from fxip import fxip_decode, jpeg_oracle
from watermark import WatermarkError, algorithm_fingerprint, clean_jpeg


class ImageServiceError(RuntimeError):
    def __init__(self, code, photo_id, message, details=None):
        super().__init__(message)
        self.code = code
        self.photo_id = photo_id
        self.details = details or {}

    def to_dict(self):
        return {
            'photo_id': self.photo_id,
            'code': self.code,
            'message': str(self),
            'details': self.details,
        }


def _sanitize(value):
    return ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in str(value))


class ImageService:
    def __init__(self, cache_dir, workers=2):
        self.cache_dir = os.path.abspath(cache_dir)
        self.workers = max(1, int(workers))
        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='watermark')
        self._lock = threading.Lock()
        self._inflight = {}
        self._photo_locks = {}
        self.algorithm_hash = algorithm_fingerprint()
        os.makedirs(self.cache_dir, exist_ok=True)

    @staticmethod
    def _source_stat(path):
        if not path:
            return {'path': '', 'missing': True}
        absolute = os.path.abspath(path)
        try:
            stat = os.stat(absolute)
        except OSError:
            return {'path': absolute, 'missing': True}
        return {
            'path': absolute,
            'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns,
        }

    def source_signature(self, photo):
        return {
            'algorithm': self.algorithm_hash,
            'refined': self._source_stat(photo.get('src_3000')),
            'original': self._source_stat(photo.get('src_o_3000')),
        }

    def fingerprint(self, photo):
        payload = json.dumps(
            self.source_signature(photo), sort_keys=True, ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()[:20]

    def revision(self, photo):
        return self.fingerprint(photo)

    def _paths(self, photo, fingerprint=None):
        fingerprint = fingerprint or self.fingerprint(photo)
        folder = os.path.join(
            self.cache_dir,
            _sanitize(photo.get('project_id', 'unknown')),
            _sanitize(photo.get('photo_id', 'unknown')),
            fingerprint,
        )
        return {
            'folder': folder,
            'master': os.path.join(folder, 'master.jpg'),
            'thumb': os.path.join(folder, '375.jpg'),
            'manifest': os.path.join(folder, 'manifest.json'),
        }

    def cached(self, photo):
        paths = self._paths(photo)
        return all(os.path.isfile(paths[key]) for key in ('master', 'thumb', 'manifest'))

    def request(self, photo):
        photo = dict(photo)
        fingerprint = self.fingerprint(photo)
        paths = self._paths(photo, fingerprint)
        if all(os.path.isfile(paths[key]) for key in ('master', 'thumb', 'manifest')):
            future = self.executor.submit(lambda: paths)
            return future

        key = '%s|%s|%s' % (
            photo.get('project_id'), photo.get('photo_id'), fingerprint,
        )
        photo_lock = self._photo_lock(photo)
        with self._lock:
            future = self._inflight.get(key)
            if future is None:
                future = self.executor.submit(self._build_locked, photo, fingerprint, photo_lock)
                self._inflight[key] = future
                created = True
            else:
                created = False
        if created:
            future.add_done_callback(lambda _, job_key=key: self._drop_inflight(job_key))
        return future

    def _drop_inflight(self, key):
        with self._lock:
            self._inflight.pop(key, None)

    def _photo_lock(self, photo):
        key = '%s|%s' % (photo.get('project_id'), photo.get('photo_id'))
        with self._lock:
            return self._photo_locks.setdefault(key, threading.Lock())

    def _build_locked(self, photo, fingerprint, photo_lock):
        with photo_lock:
            return self._build(photo, fingerprint)

    def request_snapshot(self, photo, target_path):
        """Build and copy a stable master for a ZIP job before cache eviction."""
        return self.executor.submit(self._snapshot, dict(photo), os.path.abspath(target_path))

    def _snapshot(self, photo, target_path):
        fingerprint = self.fingerprint(photo)
        with self._photo_lock(photo):
            paths = self._build(photo, fingerprint)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            tmp = '%s.%s.part' % (target_path, uuid.uuid4().hex)
            try:
                shutil.copyfile(paths['master'], tmp)
                os.replace(tmp, target_path)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return target_path

    def ensure(self, photo, size):
        paths = self.request(photo).result()
        return paths['thumb'] if int(size) == 375 else paths['master']

    @staticmethod
    def _decode_fxip(path, photo_id, role):
        if not path or not os.path.isfile(path):
            raise ImageServiceError(
                'SOURCE_MISSING', photo_id, '%s文件不存在' % role, {'path': path or ''}
            )
        try:
            data = fxip_decode.decode(path)
        except Exception as exc:
            raise ImageServiceError(
                'FXIP_DECODE_FAILED', photo_id, '%s解码失败' % role,
                {'error': str(exc)},
            ) from exc
        error = jpeg_oracle.validate_jpeg(data)
        if error is not None:
            raise ImageServiceError(
                'JPEG_INVALID', photo_id, '%s解码结果不是有效 JPEG' % role,
                {'error': str(error)},
            )
        return data

    @staticmethod
    def _thumbnail(master_jpeg):
        with Image.open(io.BytesIO(master_jpeg)) as source:
            source.thumbnail((375, 375), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            options = {'format': 'JPEG', 'quality': 94, 'subsampling': 0}
            for key in ('exif', 'icc_profile', 'dpi'):
                if source.info.get(key) is not None:
                    options[key] = source.info[key]
            source.save(output, **options)
            return output.getvalue()

    @staticmethod
    def _atomic_write(path, data):
        tmp = '%s.%s.part' % (path, uuid.uuid4().hex)
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _build(self, photo, fingerprint):
        photo_id = photo.get('photo_id', 'unknown')
        paths = self._paths(photo, fingerprint)
        if all(os.path.isfile(paths[key]) for key in ('master', 'thumb', 'manifest')):
            return paths
        os.makedirs(paths['folder'], exist_ok=True)

        refined = self._decode_fxip(photo.get('src_3000'), photo_id, '精修图')
        original = self._decode_fxip(photo.get('src_o_3000'), photo_id, '原图')
        try:
            master, report = clean_jpeg(refined, original)
        except WatermarkError as exc:
            raise ImageServiceError(exc.code, photo_id, str(exc), exc.details) from exc

        if jpeg_oracle.validate_jpeg(master) is not None:
            raise ImageServiceError('ENCODE_FAILED', photo_id, '去水印成品 JPEG 校验失败')
        thumb = self._thumbnail(master)
        if jpeg_oracle.validate_jpeg(thumb) is not None:
            raise ImageServiceError('ENCODE_FAILED', photo_id, '缩略图 JPEG 校验失败')

        signature = self.source_signature(photo)
        current_fingerprint = self.fingerprint(photo)
        if current_fingerprint != fingerprint:
            raise ImageServiceError(
                'SOURCE_CHANGED', photo_id, '处理期间源文件发生变化，请重试'
            )

        self._atomic_write(paths['master'], master)
        self._atomic_write(paths['thumb'], thumb)
        manifest = {
            'photo_id': photo_id,
            'project_id': photo.get('project_id'),
            'fingerprint': fingerprint,
            'sources': signature,
            'report': report.to_dict(),
        }
        self._atomic_write(
            paths['manifest'],
            json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'),
        )
        photo_cache = os.path.dirname(paths['folder'])
        for name in os.listdir(photo_cache):
            old_folder = os.path.join(photo_cache, name)
            if old_folder == paths['folder']:
                continue
            if os.path.isfile(os.path.join(old_folder, 'manifest.json')):
                try:
                    shutil.rmtree(old_folder)
                except OSError:
                    pass
        return paths

    def shutdown(self):
        self.executor.shutdown(wait=False, cancel_futures=False)
