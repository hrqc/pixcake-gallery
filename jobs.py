# -*- coding: utf-8 -*-
"""Asynchronous clean-photo ZIP export jobs with progress reporting."""

from __future__ import annotations

import os
import secrets
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import db
from image_service import ImageServiceError


class ExportManager:
    def __init__(self, image_service, data_dir, ttl_seconds=6 * 3600,
                 max_jobs=100, max_active=20):
        self.image_service = image_service
        self.export_dir = os.path.join(data_dir, 'exports')
        self._lock = threading.Lock()
        self._jobs = {}
        self._dedupe = {}
        self._ttl_seconds = max(300, int(ttl_seconds))
        self._max_jobs = max(10, int(max_jobs))
        self._max_active = max(2, int(max_active))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='export')
        os.makedirs(self.export_dir, exist_ok=True)
        with self._lock:
            self._cleanup_locked()

    def start(self, pid, selected_only=True, photo_ids=None, filename_prefix='选中照片',
              context=None, dedupe_key=None):
        job_args = (bool(selected_only), list(photo_ids) if photo_ids is not None else None)
        with self._lock:
            self._cleanup_locked()
            if dedupe_key:
                existing_id = self._dedupe.get(dedupe_key)
                existing = self._jobs.get(existing_id)
                if (existing and existing['status'] == 'ready' and
                        not os.path.isfile(existing.get('path') or '')):
                    self._remove_job_locked(existing_id)
                    existing = None
                if existing and existing['status'] in ('queued', 'running', 'ready'):
                    return self._public(existing)
            active = sum(
                job['status'] in ('queued', 'running') for job in self._jobs.values()
            )
            if active >= self._max_active:
                return {
                    'id': '', 'project_id': pid, 'status': 'failed', 'stage': 'failed',
                    'processed': 0, 'total': 0, 'zipped': 0,
                    'failures': [{'code': 'QUEUE_FULL', 'message': '处理队列已满，请稍后重试'}],
                    'filename': None, 'created_at': time.time(),
                }
            job_id = secrets.token_urlsafe(12)
            job = {
                'id': job_id,
                'project_id': pid,
                'status': 'queued',
                'stage': 'queued',
                'processed': 0,
                'total': 0,
                'zipped': 0,
                'failures': [],
                'path': None,
                'filename': None,
                'created_at': time.time(),
                'context': dict(context or {}),
                'dedupe_key': dedupe_key,
                'filename_prefix': filename_prefix,
            }
            self._jobs[job_id] = job
            if dedupe_key:
                self._dedupe[dedupe_key] = job_id
        try:
            self._executor.submit(self._run_guarded, job_id, *job_args)
        except Exception as exc:
            self._update(
                job_id, status='failed', stage='failed', finished_at=time.time(),
                failures=[{'code': 'EXPORT_START_FAILED', 'message': str(exc)}],
            )
        return self.status(job_id)

    def _remove_job_locked(self, job_id):
        job = self._jobs.pop(job_id, None)
        if not job:
            return
        dedupe_key = job.get('dedupe_key')
        if dedupe_key and self._dedupe.get(dedupe_key) == job_id:
            self._dedupe.pop(dedupe_key, None)
        for path in (job.get('path'), os.path.join(self.export_dir, '%s.zip.part' % job_id)):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        shutil.rmtree(os.path.join(self.export_dir, '%s.stage' % job_id), ignore_errors=True)

    def _cleanup_locked(self):
        cutoff = time.time() - self._ttl_seconds
        completed = [
            job for job in self._jobs.values()
            if job['status'] in ('ready', 'failed')
        ]
        expired = [job for job in completed if job.get('finished_at', job['created_at']) < cutoff]
        excess = max(0, len(self._jobs) - self._max_jobs)
        oldest = sorted(completed, key=lambda job: job['created_at'])[:excess]
        for job_id in {job['id'] for job in expired + oldest}:
            self._remove_job_locked(job_id)
        referenced = {job.get('path') for job in self._jobs.values() if job.get('path')}
        for name in os.listdir(self.export_dir):
            path = os.path.join(self.export_dir, name)
            try:
                stale = os.path.isfile(path) and os.path.getmtime(path) < cutoff
            except OSError:
                continue
            if stale and path not in referenced:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _run_guarded(self, job_id, selected_only, photo_ids):
        try:
            self._run(job_id, selected_only, photo_ids)
        except Exception as exc:
            self._update(
                job_id, status='failed', stage='failed', finished_at=time.time(),
                failures=[{'code': 'EXPORT_FAILED', 'message': str(exc)}],
            )
        finally:
            shutil.rmtree(os.path.join(self.export_dir, '%s.stage' % job_id), ignore_errors=True)

    def _update(self, job_id, **changes):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.update(changes)

    def _run(self, job_id, selected_only, photo_ids):
        with self._lock:
            pid = self._jobs[job_id]['project_id']
        missing = []
        if photo_ids is None:
            photos = db.all_selected(pid) if selected_only else db.list_photos(pid)
        else:
            photos = []
            for photo_id in photo_ids:
                photo = db.get_photo('%s|%s' % (pid, photo_id))
                if not photo or not photo.get('on_disk'):
                    missing.append({
                        'photo_id': photo_id,
                        'code': 'PHOTO_MISSING',
                        'message': '照片不存在或已不在磁盘上',
                    })
                else:
                    photos.append(photo)
        self._update(job_id, status='running', stage='processing', total=len(photos))
        if missing:
            self._update(
                job_id, status='failed', stage='failed', failures=missing,
                total=len(photo_ids), processed=len(missing), finished_at=time.time(),
            )
            return
        if not photos:
            self._update(
                job_id, status='failed', stage='failed',
                finished_at=time.time(),
                failures=[{'code': 'NO_PHOTOS', 'message': '没有可导出的照片'}],
            )
            return

        stage_dir = os.path.join(self.export_dir, '%s.stage' % job_id)
        os.makedirs(stage_dir, exist_ok=True)
        request_snapshot = getattr(self.image_service, 'request_snapshot', None)
        future_to_photo = {}
        for index, photo in enumerate(photos, 1):
            if request_snapshot:
                target = os.path.join(stage_dir, '%06d.jpg' % index)
                future = request_snapshot(photo, target)
            else:
                future = self.image_service.request(photo)
            future_to_photo[future] = photo
        artifacts = {}
        failures = []
        processed = 0
        for future in as_completed(future_to_photo):
            photo = future_to_photo[future]
            try:
                result = future.result()
                artifacts[photo['key']] = result if isinstance(result, str) else result['master']
            except ImageServiceError as exc:
                failures.append(exc.to_dict())
            except Exception as exc:
                failures.append({
                    'photo_id': photo.get('photo_id'),
                    'code': 'UNEXPECTED_ERROR',
                    'message': str(exc),
                })
            processed += 1
            self._update(job_id, processed=processed, failures=list(failures))

        # 部分失败: 跳过失败照片, 剩余照常打包; 全部失败才整批失败
        if not artifacts:
            self._update(
                job_id, status='failed', stage='failed', finished_at=time.time(),
                failures=list(failures),
            )
            return

        required_bytes = sum(os.path.getsize(path) for path in artifacts.values())
        free_bytes = shutil.disk_usage(self.export_dir).free
        reserve_bytes = max(256 * 1024 * 1024, required_bytes // 20)
        if free_bytes < required_bytes + reserve_bytes:
            self._update(
                job_id, status='failed', stage='failed', finished_at=time.time(),
                failures=[{'code': 'DISK_SPACE', 'message': '磁盘剩余空间不足，无法生成导出文件'}],
            )
            return

        project = db.get_project(pid)
        project_name = ((project.get('name') or project.get('album_id') or pid)
                        if project else pid)
        with self._lock:
            prefix = self._jobs[job_id]['filename_prefix']
        filename = '%s_%s_%s.zip' % (
            prefix, project_name, datetime.now().strftime('%Y%m%d_%H%M')
        )
        part_path = os.path.join(self.export_dir, '%s.zip.part' % job_id)
        final_path = os.path.join(self.export_dir, '%s.zip' % job_id)
        try:
            self._update(job_id, stage='zipping')
            with zipfile.ZipFile(part_path, 'w', zipfile.ZIP_STORED) as archive:
                for index, photo in enumerate(photos, 1):
                    source = artifacts.get(photo['key'])
                    if not source:
                        continue
                    safe_id = ''.join(
                        ch if ch.isalnum() or ch in '_-' else '_'
                        for ch in str(photo['photo_id'])
                    )
                    archive.write(
                        source,
                        '%03d_%s.jpg' % (index, safe_id),
                    )
                    self._update(job_id, zipped=index)
            os.replace(part_path, final_path)
        except Exception as exc:
            try:
                os.remove(part_path)
            except OSError:
                pass
            self._update(
                job_id, status='failed', stage='failed',
                finished_at=time.time(),
                failures=[{'code': 'ZIP_FAILED', 'message': str(exc)}],
            )
            return

        self._update(
            job_id, status='ready', stage='ready', path=final_path,
            filename=filename, finished_at=time.time(),
            failures=list(failures),
        )

    def status(self, job_id):
        with self._lock:
            self._cleanup_locked()
            job = self._jobs.get(job_id)
            if not job:
                return None
            return self._public(job)

    @staticmethod
    def _public(job):
        hidden = {'path', 'context', 'dedupe_key', 'filename_prefix'}
        return {key: value for key, value in job.items() if key not in hidden}

    def matches(self, job_id, **expected):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            context = job.get('context') or {}
            return all(context.get(key) == value for key, value in expected.items())

    def download(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job['status'] != 'ready':
                return None
            if not os.path.isfile(job.get('path') or ''):
                job.update(
                    status='failed', stage='failed', finished_at=time.time(),
                    failures=[{'code': 'EXPORT_MISSING', 'message': '导出文件已失效，请重试'}],
                )
                return None
            return job['path'], job['filename']

    def shutdown(self):
        self._executor.shutdown(wait=True, cancel_futures=True)
