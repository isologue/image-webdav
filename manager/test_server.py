import base64
import http.client
import json
import threading
import time
import unittest
from urllib.parse import quote

import uvicorn
import app
from auth import update_settings


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = uvicorn.Server(uvicorn.Config(app.app, host='127.0.0.1', port=8001, log_level='error'))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError('Test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None, cookie=None, dav=False):
        headers = dict(headers or {})
        if cookie:
            headers['Cookie'] = cookie
        if dav:
            headers['Authorization'] = 'Basic ' + base64.b64encode(b'admin:admin2026').decode()
        if isinstance(body, dict):
            body = json.dumps(body)
            headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'image-manager'})
        conn = http.client.HTTPConnection('127.0.0.1', 8001, timeout=5)
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def login(self):
        status, headers, _ = self.request('POST', '/admin/api/login', {'username': 'admin', 'password': 'admin2026'})
        self.assertEqual(status, 200)
        self.assertIn('httponly', headers['set-cookie'].lower())
        return headers['set-cookie'].split(';')[0]

    def test_login_and_admin_protection(self):
        self.assertEqual(self.request('GET', '/admin/')[0], 303)
        self.assertEqual(self.request('GET', '/admin/login')[0], 200)
        for route in ('settings', 'files', 'cleanup'):
            self.assertEqual(self.request('GET', '/admin/api/' + route)[0], 401)
        self.assertEqual(self.request('GET', '/api/settings')[0], 404)
        self.assertEqual(self.request('POST', '/admin/api/login', {'username': 'admin', 'password': 'wrong'})[0], 401)
        cookie = self.login()
        self.assertEqual(self.request('GET', '/admin/', cookie=cookie)[0], 200)
        status, _, body = self.request('GET', '/admin/api/settings', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn('password_hash', json.loads(body))
        self.assertEqual(self.request('POST', '/admin/api/cleanup/run', {'hours': 0}, cookie=cookie)[0], 422)
        self.assertEqual(self.request('POST', '/admin/api/logout', headers={'X-Requested-With': 'image-manager'}, cookie=cookie)[0], 200)

    def test_dav_upload_public_image_and_delete(self):
        self.assertEqual(self.request('PROPFIND', '/dav/', headers={'Depth': '0'})[0], 401)
        for path in ('/dav/chatgpt2api', '/dav/chatgpt2api/images', '/dav/chatgpt2api/images/test'):
            self.assertIn(self.request('MKCOL', path, dav=True)[0], (201, 405))
        rel = 'test/' + quote('\u56fe\u7247.png')
        path = '/dav/chatgpt2api/images/' + rel
        payload = b'\x89PNG\r\n\x1a\nimage test'
        self.assertEqual(self.request('PUT', path, payload)[0], 401)
        self.assertEqual(self.request('PUT', path, payload, dav=True)[0], 201)
        self.assertEqual(self.request('GET', path, dav=True)[2], payload)
        self.assertEqual(self.request('PROPFIND', '/dav/chatgpt2api/images/test/', headers={'Depth': '1'}, dav=True)[0], 207)
        self.assertEqual(self.request('GET', '/images/' + rel)[2], payload)
        self.assertEqual(self.request('HEAD', '/images/' + rel)[0], 200)
        self.assertEqual(self.request('PUT', path, payload + b'2', dav=True)[0], 204)
        cookie = self.login()
        status, _, body = self.request('GET', '/admin/api/files?path=test', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['total'], 1)
        self.assertEqual(self.request('DELETE', path, dav=True)[0], 204)
        self.assertEqual(self.request('GET', '/images/' + rel)[0], 404)

    def test_traversal_and_symlinks(self):
        (app.DATA / 'outside').symlink_to('/config', target_is_directory=True)
        try:
            self.assertIn(self.request('GET', '/images/outside/settings.json')[0], (400, 404))
            self.assertIn(self.request('GET', '/dav/chatgpt2api/images/outside/settings.json', dav=True)[0], (403, 404))
            self.assertIn(self.request('GET', '/dav/%2e%2e/config/settings.json', dav=True)[0], (403, 404))
        finally:
            (app.DATA / 'outside').unlink()

    def test_password_change_invalidates_session_and_dav_password(self):
        cookie = self.login()
        try:
            status, _, _ = self.request('POST', '/admin/api/settings',
                {'username': 'admin', 'password': 'changed2026', 'public_base': ''}, cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(self.request('GET', '/admin/api/files', cookie=cookie)[0], 401)
            self.assertEqual(self.request('PROPFIND', '/dav/', headers={'Depth': '0'}, dav=True)[0], 401)
        finally:
            update_settings('admin', 'admin2026', '')


if __name__ == '__main__':
    unittest.main()
