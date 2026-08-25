# -*- coding: utf-8 -*-
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import Future
from unittest import mock

from PIL import Image

import db
from fxip import fxip_decode
from image_service import ImageService, ImageServiceError
from gallery import _delivery_job_public
from jobs import ExportManager


class DatabaseMigrationTests(unittest.TestCase):
    def test_legacy_database_gets_original_source_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'gallery.db')
            conn = sqlite3.connect(path)
            conn.execute(
                'CREATE TABLE photos('
                'key TEXT PRIMARY KEY, project_id TEXT, photo_id TEXT, selected INTEGER)'
            )
            conn.commit()
            conn.close()
            old_path = db.DB_FILE
            try:
                db.DB_FILE = path
                db.init_db()
                conn = sqlite3.connect(path)
                columns = {row[1] for row in conn.execute('PRAGMA table_info(photos)')}
                version = conn.execute('PRAGMA user_version').fetchone()[0]
                conn.close()
            finally:
                db.DB_FILE = old_path
            self.assertTrue({'src_o_3000', 'src_o_375', 'mtime_o_3000', 'mtime_o_375'} <= columns)
            self.assertEqual(version, 3)   # v3 = 多租户 (photographers / license_keys.tenant / projects.owner)


class ImageServiceUnitTests(unittest.TestCase):
    def test_source_change_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            refined = os.path.join(folder, 'e')
            original = os.path.join(folder, 'o')
            with open(refined, 'wb') as output:
                output.write(b'e')
            with open(original, 'wb') as output:
                output.write(b'o')
            service = ImageService(os.path.join(folder, 'cache'), workers=1)
            try:
                photo = {'project_id': 'p', 'photo_id': '1', 'src_3000': refined, 'src_o_3000': original}
                before = service.fingerprint(photo)
                stat = os.stat(original)
                os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                self.assertNotEqual(before, service.fingerprint(photo))
            finally:
                service.shutdown()

    def test_same_photo_requests_share_one_inflight_future(self):
        with tempfile.TemporaryDirectory() as folder:
            refined = os.path.join(folder, 'e')
            original = os.path.join(folder, 'o')
            with open(refined, 'wb') as output:
                output.write(b'e')
            with open(original, 'wb') as output:
                output.write(b'o')
            service = ImageService(os.path.join(folder, 'cache'), workers=1)
            photo = {'project_id': 'p', 'photo_id': '1', 'src_3000': refined, 'src_o_3000': original}

            def slow_build(_photo, _fingerprint):
                time.sleep(0.1)
                return {'master': 'm', 'thumb': 't', 'manifest': 'j'}

            try:
                with mock.patch.object(service, '_build', side_effect=slow_build) as build:
                    first = service.request(photo)
                    second = service.request(photo)
                    self.assertIs(first, second)
                    self.assertEqual(first.result()['master'], 'm')
                    self.assertEqual(build.call_count, 1)
            finally:
                service.shutdown()

    def test_immediately_completed_future_does_not_deadlock(self):
        class ImmediateExecutor:
            def submit(self, function, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(function(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                return future

            def shutdown(self, **_kwargs):
                pass

        with tempfile.TemporaryDirectory() as folder:
            refined = os.path.join(folder, 'e')
            original = os.path.join(folder, 'o')
            for path in (refined, original):
                with open(path, 'wb') as output:
                    output.write(b'x')
            service = ImageService(os.path.join(folder, 'cache'), workers=1)
            service.executor.shutdown(wait=True)
            service.executor = ImmediateExecutor()
            photo = {'project_id': 'p', 'photo_id': '1', 'src_3000': refined, 'src_o_3000': original}
            with mock.patch.object(
                    service, '_build', return_value={'master': 'm', 'thumb': 't', 'manifest': 'j'}):
                self.assertEqual(service.request(photo).result()['master'], 'm')
                self.assertEqual(service._inflight, {})

    def test_snapshot_survives_cache_file_eviction(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, 'master.jpg')
            target = os.path.join(folder, 'stage', 'photo.jpg')
            with open(source, 'wb') as output:
                output.write(b'clean jpeg bytes')
            service = ImageService(os.path.join(folder, 'cache'), workers=1)
            photo = {'project_id': 'p', 'photo_id': '1'}
            paths = {'master': source, 'thumb': source, 'manifest': source}
            try:
                with mock.patch.object(service, '_build', return_value=paths):
                    self.assertEqual(service.request_snapshot(photo, target).result(), target)
                os.remove(source)
                with open(target, 'rb') as saved:
                    self.assertEqual(saved.read(), b'clean jpeg bytes')
            finally:
                service.shutdown()


class ExportManagerTests(unittest.TestCase):
    def test_export_uses_stable_snapshots_and_removes_staging(self):
        class SnapshotImageService:
            def request_snapshot(self, photo, target):
                future = Future()
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, 'wb') as output:
                    output.write(photo['photo_id'].encode('ascii'))
                future.set_result(target)
                return future

        photo = {'key': 'p|1', 'project_id': 'p', 'photo_id': '1', 'on_disk': 1}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(db, 'get_photo', return_value=photo), \
                mock.patch.object(db, 'get_project', return_value={'name': 'test'}):
            manager = ExportManager(SnapshotImageService(), folder)
            job = manager.start('p', photo_ids=['1'])
            deadline = time.time() + 3
            while time.time() < deadline and manager.status(job['id'])['status'] in ('queued', 'running'):
                time.sleep(0.02)
            status = manager.status(job['id'])
            self.assertEqual(status['status'], 'ready')
            path, _ = manager.download(job['id'])
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read('001_1.jpg'), b'1')
            self.assertFalse(os.path.exists(os.path.join(manager.export_dir, '%s.stage' % job['id'])))
            manager.shutdown()

    def test_partial_failure_skips_bad_photo_and_zips_rest(self):
        class FakeImageService:
            def request(self, photo):
                future = Future()
                if photo['photo_id'] == 'bad':
                    future.set_exception(ImageServiceError('TEST_FAILURE', 'bad', '测试失败'))
                else:
                    future.set_result({'master': __file__})
                return future

        photos = {
            'p|good': {'key': 'p|good', 'project_id': 'p', 'photo_id': 'good', 'on_disk': 1},
            'p|bad': {'key': 'p|bad', 'project_id': 'p', 'photo_id': 'bad', 'on_disk': 1},
        }
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(db, 'get_photo', side_effect=lambda key: photos.get(key)), \
                mock.patch.object(db, 'get_project', return_value={'name': 'test'}):
            manager = ExportManager(FakeImageService(), folder)
            job = manager.start('p', photo_ids=['good', 'bad'])
            deadline = time.time() + 3
            while time.time() < deadline:
                status = manager.status(job['id'])
                if status['status'] not in ('queued', 'running'):
                    break
                time.sleep(0.02)
            self.assertEqual(status['status'], 'ready')  # 部分失败仍交付
            self.assertEqual([failure['photo_id'] for failure in status['failures']], ['bad'])
            path, _ = manager.download(job['id'])
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.namelist(), ['001_good.jpg'])  # 只打包成功的
            self.assertFalse(os.path.exists(os.path.join(manager.export_dir, '%s.stage' % job['id'])))
            manager.shutdown()

    def test_unexpected_exception_reaches_failed_terminal_state(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(db, 'get_photo', side_effect=RuntimeError('database down')):
            manager = ExportManager(mock.Mock(), folder)
            job = manager.start('p', photo_ids=['1'])
            deadline = time.time() + 3
            while time.time() < deadline and manager.status(job['id'])['status'] in ('queued', 'running'):
                time.sleep(0.02)
            status = manager.status(job['id'])
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['failures'][0]['code'], 'EXPORT_FAILED')
            manager.shutdown()

    def test_concurrent_deduplication_creates_one_job(self):
        class FakeImageService:
            def request(self, _photo):
                future = Future()
                future.set_exception(ImageServiceError('TEST', '1', 'stop'))
                return future

        photo = {'key': 'p|1', 'project_id': 'p', 'photo_id': '1', 'on_disk': 1}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(db, 'get_photo', return_value=photo):
            manager = ExportManager(FakeImageService(), folder)
            barrier = threading.Barrier(8)
            ids = []

            def start():
                barrier.wait()
                ids.append(manager.start('p', photo_ids=['1'], dedupe_key='same')['id'])

            threads = [threading.Thread(target=start) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(set(ids)), 1)
            manager.shutdown()


class DeliveryPublicResponseTests(unittest.TestCase):
    def test_customer_job_response_hides_internal_failure_details(self):
        public = _delivery_job_public({
            'id': 'j',
            'failures': [{
                'photo_id': '1', 'code': 'JPEG_INVALID', 'message': 'D:\\secret\\file',
                'details': {'path': 'D:\\secret\\file'},
            }],
        })
        self.assertEqual(public['failures'], [{
            'photo_id': '1', 'code': 'JPEG_INVALID', 'message': '该照片处理失败',
        }])


class RealWatermarkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.samples = {}
        for project in db.list_projects():
            for photo in db.list_photos(project['id']):
                try:
                    refined = fxip_decode.decode(photo['src_3000'])
                    with Image.open(io.BytesIO(refined)) as image:
                        orientation = 'landscape' if image.width > image.height else 'portrait'
                except Exception:
                    continue
                cls.samples.setdefault(orientation, (photo, refined))
                if len(cls.samples) == 2:
                    return

    def test_portrait_and_landscape_keep_metadata_and_cache(self):
        if len(self.samples) != 2:
            self.skipTest('当前图库没有同时包含横图和竖图')
        with tempfile.TemporaryDirectory() as folder:
            service = ImageService(folder, workers=2)
            try:
                for orientation in ('portrait', 'landscape'):
                    photo, refined = self.samples[orientation]
                    path = service.ensure(photo, 3000)
                    with Image.open(io.BytesIO(refined)) as before, Image.open(path) as after:
                        self.assertEqual(before.size, after.size)
                        self.assertEqual(before.info.get('icc_profile'), after.info.get('icc_profile'))
                        self.assertEqual(before.info.get('exif'), after.info.get('exif'))
                    mtime = os.stat(path).st_mtime_ns
                    self.assertEqual(path, service.ensure(photo, 3000))
                    self.assertEqual(mtime, os.stat(path).st_mtime_ns)
                    with Image.open(service.ensure(photo, 375)) as thumb:
                        self.assertLessEqual(max(thumb.size), 375)
            finally:
                service.shutdown()

    def test_low_contrast_watermarks_are_removed(self):
        photos = {
            photo['photo_id']: photo
            for project in db.list_projects()
            for photo in db.list_photos(project['id'])
            if photo['photo_id'] in ('1A0A3260', '1A0A3270', '1A0A3271')
        }
        if len(photos) != 3:
            self.skipTest('当前图库没有低对比测试样本')
        with tempfile.TemporaryDirectory() as folder:
            service = ImageService(folder, workers=2)
            try:
                paths = [service.request(photo) for photo in photos.values()]
                for future in paths:
                    result = future.result()
                    self.assertTrue(os.path.isfile(result['master']))
                    with open(result['manifest'], 'r', encoding='utf-8') as source:
                        report = json.load(source)['report']
                    self.assertEqual(report['state'], 'cleaned')
                    self.assertLess(report['after_score'], report['before_score'] * 0.65)
            finally:
                service.shutdown()

    def test_corrupt_refined_source_is_rejected(self):
        photo = next((
            photo
            for project in db.list_projects()
            for photo in db.list_photos(project['id'])
            if photo['photo_id'] == '1A0A3294'
        ), None)
        if not photo:
            self.skipTest('当前图库没有损坏源测试样本')
        with tempfile.TemporaryDirectory() as folder:
            service = ImageService(folder, workers=1)
            try:
                with self.assertRaises(ImageServiceError) as caught:
                    service.ensure(photo, 3000)
                self.assertEqual(caught.exception.code, 'JPEG_INVALID')
            finally:
                service.shutdown()


if __name__ == '__main__':
    unittest.main()
