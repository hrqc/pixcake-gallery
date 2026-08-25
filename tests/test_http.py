# -*- coding: utf-8 -*-
import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import gallery


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), gallery.Handler)
        self.server.token = 'admin-token'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_delivery_code_cannot_fetch_full_resolution_image(self):
        with mock.patch.object(gallery, '_delivery_code_ok', return_value=True):
            status, _ = self.request('GET', '/img/project/photo/3000.jpg?k=delivery-code')
        self.assertEqual(status, 403)

    def test_oversized_json_body_is_rejected_before_parsing(self):
        body = b'x' * (1024 * 1024 + 1)
        status, _ = self.request(
            'POST', '/api/delivery/select', body,
            {'Content-Type': 'application/json', 'Content-Length': str(len(body))},
        )
        self.assertEqual(status, 413)


if __name__ == '__main__':
    unittest.main()
